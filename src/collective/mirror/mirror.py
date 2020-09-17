from .interfaces import ICollectiveMirrorLayer
from Acquisition import aq_base
from Acquisition import aq_chain
from Acquisition import aq_parent
from OFS.interfaces import IObjectWillBeAddedEvent
from OFS.interfaces import IObjectWillBeRemovedEvent
from persistent.list import PersistentList
from plone.app.multilingual.dx.interfaces import IDexterityTranslatable
from plone.app.multilingual.interfaces import ITG
from plone.app.vocabularies.catalog import CatalogVocabulary
from plone.app.vocabularies.utils import parseQueryString
from plone.app.z3cform.widget import RelatedItemsFieldWidget
from plone.autoform import directives
from plone.dexterity.content import Container
from plone.dexterity.content import DexterityContent
from plone.indexer import indexer
from plone.supermodel import model
from plone.uuid.interfaces import ATTRIBUTE_NAME
from plone.uuid.interfaces import IAttributeUUID
from plone.uuid.interfaces import IUUID
from Products.CMFCore.interfaces import ISiteRoot
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.interfaces import ILanguage
from Products.CMFPlone.interfaces import IPloneSiteRoot
from z3c.form.interfaces import IAddForm
from z3c.relationfield import RelationChoice
from z3c.relationfield.relation import RelationValue
from zope.annotation.interfaces import IAnnotations
from zope.component import adapter
from zope.component import getSiteManager
from zope.component import getUtility
from zope.globalrequest import getRequest
from zope.interface import implementer
from zope.intid.interfaces import IIntIds
from zope.lifecycleevent.interfaces import IObjectAddedEvent
from zope.lifecycleevent.interfaces import IObjectRemovedEvent
from zope.schema.interfaces import IVocabularyFactory


class IMirror(model.Schema):

    master_rel = RelationChoice(
        title=u'Master container',
        vocabulary='collective.mirror.vocabularies.Catalog',
    )
    directives.widget(
        'master_rel',
        RelatedItemsFieldWidget,
        pattern_options={
            'selectableTypes': ['Folder'],
        },
    )
    directives.omitted('master_rel')
    directives.no_omit(IAddForm, 'master_rel')


MIRRORS_ATTR = '_collective_mirrors'


def ensure_mirrors_attr(master):
    mirrors = getattr(master, MIRRORS_ATTR, None)
    if mirrors is None:
        mirrors = PersistentList()
        setattr(aq_base(master), MIRRORS_ATTR, mirrors)
    return mirrors


@implementer(IMirror)
class Mirror(Container):
    """Container that mirrors the content of a master container."""

    _master = None

    @property
    def master_rel(self):
        return self._master

    @master_rel.setter
    def master_rel(self, value):
        if self._master:
            old_master = self._master.to_object
            if old_master:
                mirrors = getattr(old_master, MIRRORS_ATTR, ())
                if mirrors:
                    mirrors.remove(IUUID(self))
                    if not mirrors:
                        delattr(old_master, MIRRORS_ATTR)

        self._master = value

        # modelled after plone/app/multilingual/content/lif.py
        master = value.to_object

        self._tree = master._tree
        self._count = master._count
        self._mt_index = master._mt_index
        self.getOrdering()._set_order(master.getOrdering()._order(create=True))

        mirrors = ensure_mirrors_attr(master)
        setattr(aq_base(self), MIRRORS_ATTR, mirrors)

        try:
            mirrors.append(IUUID(self))
        except TypeError:
            # self cannot yet be adapted to IUUID while being added
            pass

    @property
    def master(self):
        if self.master_rel:
            return self.master_rel.to_object

    @master.setter
    def master(self, obj):
        obj_id = getUtility(IIntIds).getId(obj)
        self.master_rel = RelationValue(obj_id)


def add_mirror_id_to_master_after_adding(mirror, event):
    if mirror.master is not None:
        mirrors = ensure_mirrors_attr(mirror.master)
        mirrors.append(IUUID(mirror))


@implementer(IVocabularyFactory)
class CatalogVocabularyFactory:
    """Like plone.app.vocabularies.catalog.CatalogVocabularyFactory but without navroot
    """
    def __call__(self, context, query=None):
        parsed = {}
        if query:
            parsed = parseQueryString(context, query['criteria'])
            if 'sort_on' in query:
                parsed['sort_on'] = query['sort_on']
            if 'sort_order' in query:
                parsed['sort_order'] = str(query['sort_order'])

        return CatalogVocabulary.fromItems(parsed, context)


# We don't adapt IDexterityContent as we really target IAttributeUUID, which is
# implemented by the type but not extended by the interface. We cannot adapt
# IAttributeUUID directly either, since plone.app.multilingual already overrides such an
# adapter and we need to reuse it. Also, ZCA configuration won't let us compete with the
# override anyway.
@implementer(IUUID)
@adapter(DexterityContent)
def attributeUUID(context):
    # context = IAttributeUUID(context)
    sm = getSiteManager()
    uuid = sm.adapters.lookup((IAttributeUUID,), IUUID)
    for element in aq_chain(context)[1:]:
        if IMirror.providedBy(element):
            context_uuid = uuid(aq_base(context)) or ''
            mirror_uuid = uuid(aq_base(element)) or ''
            return f'{context_uuid}-mirrored-{mirror_uuid}'
    else:
        return uuid(context)


@indexer(IDexterityTranslatable)
def itgIndexer(obj):
    for element in aq_chain(obj)[1:]:
        if IMirror.providedBy(element):
            return None
    else:
        return ITG(obj, None)


@indexer(IDexterityTranslatable)
def LanguageIndexer(obj, **kw):
    for element in aq_chain(obj)[1:]:
        if IMirror.providedBy(element):
            return None
    else:
        return ILanguage(obj).get_language()


# modelled after plone/app/multilingual/subscriber.py


def reindex(obj, event):
    """Re-index mirrored folder content for all mirrors and master

    Mirror folder content objects are indexed once for each mirror and the
    master with different, mirror-specific UUID for each. When ever a mirrored
    folder content object is modified in some mirror or master, it must be
    re-indexed for all the other mirrors and master as well.

    """
    if not ICollectiveMirrorLayer.providedBy(getRequest()):
        return

    if IObjectRemovedEvent.providedBy(event):
        return

    master, mirror_ids = find_master(obj)
    if master is None:
        return

    # We try to access newly indexed objects via the parent in order to avoid
    # computing partial paths and starting traversal from master or mirrors.
    # However, the parent being a mirror is an exception in that mirrors' uuid
    # doesn't follow the pattern for uuids of mirrored objects.
    parent = aq_parent(obj)
    master_id = IUUID(master)
    if IMirror.providedBy(parent) or IUUID(parent) == master_id:
        parent_ids = [master_id] + mirror_ids
    else:
        parent_master_id = getattr(parent, ATTRIBUTE_NAME, None)
        if not parent_master_id:
            return
        parent_ids = [parent_master_id] + [
            f'{parent_master_id}-mirrored-{mirror_id}' for mirror_id in mirror_ids
        ]

    cat = getToolByName(obj, 'portal_catalog')
    for parent_id in parent_ids:
        brains = cat.unrestrictedSearchResults(UID=parent_id)
        for brain in brains:
            brain.getObject()[obj.id].indexObject()


def unindex(obj, event):
    """Un-index mirrored folder content for all mirrors and master

    Mirror folder content objects are indexed once for each mirror and the master with
    different, mirror-specific, UUID for each. When ever a mirrored folder content
    object is removed in some mirror or master, we must un-index it for all the other
    mirrors and master as well.

    Removing any mirror would thus unindex contents of all other mirrors and master.
    Since the order in which this subscriber is called for the folder tree objects is
    from leaf to root, we cannot easily raise some flag when deleting a mirror to
    prevent its contents from unindexing all its mirror objects. Instead, we collect all
    the uids to be unindexed and in case that the subscriber is called on the mirror in
    the end, reindex what is meant to stay in the index. The performance impact is
    somewhat mitigated by index queue optimisation.

    """
    if not ICollectiveMirrorLayer.providedBy(getRequest()):
        return

    if IObjectWillBeAddedEvent.providedBy(event):
        return
    if IPloneSiteRoot.providedBy(event.object):
        return

    UNINDEXED_KEY = 'mirror.tmp.objects_unindexed'

    if IObjectWillBeRemovedEvent.providedBy(event) and IMirror.providedBy(obj):
        getattr(obj, MIRRORS_ATTR).remove(IUUID(obj))

        objects_unindexed = IAnnotations(obj.master).pop(UNINDEXED_KEY)
        suffix = '-mirrored-' + IUUID(obj)
        for uuid, obj in objects_unindexed:
            if not uuid.endswith(suffix):
                obj.reindexObject()
        return

    master, mirror_ids = find_master(obj)
    if master is None:
        return

    uuid = IUUID(obj).split('-mirrored-')[0]
    uuids = [uuid] + [f'{uuid}-mirrored-{mirror_id}' for mirror_id in mirror_ids]
    objects_unindexed = IAnnotations(master).setdefault(UNINDEXED_KEY, set())

    cat = getToolByName(obj, 'portal_catalog')
    for uuid in uuids:
        brains = cat.unrestrictedSearchResults(UID=uuid)
        for brain in brains:
            obj = brain.getObject()
            obj.unindexObject()
            objects_unindexed.add((uuid, obj))


def find_master(obj):
    for element in aq_chain(obj)[1:]:
        if ISiteRoot.providedBy(element):
            return None, None
        mirror_ids = getattr(element, MIRRORS_ATTR, ())
        if mirror_ids:
            master = element.master if IMirror.providedBy(element) else element
            return master, mirror_ids
    else:
        return None, None


# Known issues to be addressed when moving this to a generalised package:
#
# * The mechanism for keeping track of unindexed mirror content when removing a mirror
#   is probably a bit fragile in that it relies on this being the only operation that
#   unindexes mirror content in the same request, and a non-persistent annotation on the
#   master is dropped at the transaction boundary.
#
# * When a mirror is added and the master already has a nested content hierarchy, this
#   content isn't properly indexed. This is because the events handled by the reindex
#   handler are called by the location machinery in the order from leaves to root, which
#   breaks the assumption that we can locate content to be indexed by retrieving the
#   parent from the catalog. A work-around is rebuilding the catalog.
#
# * The same goes for moving a mirror.
#
# * Changing a mirror's master after the mirror has been added isn't supported yet.
#
# * Mirrors don't interact with multilingual content in a defined way yet. The reason is
#   that plone.app.multilingual relies on there being only one object in the catalog for
#   each pair of translation group and language. We currently work around this issue by
#   indexing any mirror content without translation group or language (hence the
#   overwritten itgIndexer and LanguageIndexer). If we ever need to be able to link
#   multilingual content objects to specific mirrors of their translations, we need to
#   rethink this.
#
# * Deleting a folder while it has mirrors isn't handled yet.
