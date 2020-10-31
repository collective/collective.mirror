from .interfaces import ICollectiveMirrorLayer
from Acquisition import aq_base
from Acquisition import aq_chain
from Acquisition import aq_parent
from collections import namedtuple
from OFS.interfaces import IObjectWillBeAddedEvent
from persistent.list import PersistentList
from plone import api
from plone.app.multilingual.dx.interfaces import IDexterityTranslatable
from plone.app.multilingual.interfaces import ITG
from plone.app.vocabularies.catalog import CatalogVocabulary
from plone.app.vocabularies.utils import parseQueryString
from plone.app.z3cform.widget import RelatedItemsFieldWidget
from plone.autoform import directives
from plone.dexterity.content import Container
from plone.dexterity.interfaces import IDexterityContent
from plone.indexer import indexer
from plone.supermodel import model
from plone.uuid.interfaces import ATTRIBUTE_NAME
from plone.uuid.interfaces import IAttributeUUID
from plone.uuid.interfaces import IUUID
from Products.CMFCore.interfaces import ISiteRoot
from Products.CMFPlone.interfaces import ILanguage
from Products.CMFPlone.interfaces import IPloneSiteRoot
from z3c.relationfield import RelationChoice
from z3c.relationfield.relation import RelationValue
from zope.annotation.interfaces import IAnnotations
from zope.component import adapter
from zope.component import getSiteManager
from zope.component import getUtility
from zope.globalrequest import getRequest
from zope.interface import implementer
from zope.intid.interfaces import IIntIds
from zope.lifecycleevent.interfaces import IObjectRemovedEvent
from zope.location.interfaces import LocationError
from zope.schema.interfaces import IVocabularyFactory


class IMirror(model.Schema):

    master_rel = RelationChoice(
        title='Master container',
        vocabulary='collective.mirror.vocabularies.Catalog',
        required=False,
    )
    directives.widget(
        'master_rel',
        RelatedItemsFieldWidget,
        pattern_options={
            'selectableTypes': ['Folder'],
        },
    )


MirrorInfo = namedtuple('MirrorInfo', ('master', 'mirror', 'mirror_ids'))

NOT_MIRRORED = MirrorInfo(None, None, None)

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
        if self._master is not None:
            self._detach()

        self._master = value
        if value is not None:
            self._attach(value.to_object)

    def _attach(self, master):
        self._tree = master._tree
        self._count = master._count
        self._mt_index = master._mt_index
        ordering = self.getOrdering()
        ordering._set_order(master.getOrdering()._order(create=True))
        IAnnotations(self)[ordering.POS_KEY] = master.getOrdering()._pos(create=True)

        mirrors = ensure_mirrors_attr(master)
        setattr(aq_base(self), MIRRORS_ATTR, mirrors)

        try:
            mirrors.append(IUUID(self))
        except TypeError:
            # self cannot yet be adapted to IUUID while being added
            pass

    def _detach(self):
        cat = api.portal.get_tool('portal_catalog')
        prefix = cat.unrestrictedSearchResults(UID=IUUID(self))[0].getPath()
        content_brains = cat.unrestrictedSearchResults(path=prefix)
        for brain in content_brains:
            if brain.getPath() != prefix:
                brain.getObject().unindexObject()

        self._tree = {}
        self._count = None
        self._mt_index = {}
        ordering = self.getOrdering()
        ordering._set_order([])
        IAnnotations(self)[ordering.POS_KEY] = {}

        getattr(self, MIRRORS_ATTR).remove(IUUID(self))
        setattr(self, MIRRORS_ATTR, [])

    @property
    def master(self):
        if self.master_rel:
            return self.master_rel.to_object

    @master.setter
    def master(self, obj):
        if obj is None:
            self.master_rel = None
        else:
            obj_id = getUtility(IIntIds).getId(obj)
            self.master_rel = RelationValue(obj_id)

    def get_object(self, obj):
        return get_object_in_tree(obj, self)


def add_mirror_id_to_master_after_adding(mirror, event):
    if mirror.master is not None:
        mirrors = ensure_mirrors_attr(mirror.master)
        mirrors.append(IUUID(mirror))


def only_remove_mirror_without_master(mirror, event):
    """Make sure a mirror still attached to a master cannot be removed.

    Removing an attached mirror would cause all of its contents to be unindexed and
    deleted, which would in turn reflect in all other mirrors and the master. Even if
    we could prevent actually deleting the contents by detaching the mirror on the
    IObjectWillBeRemovedEvent, this would be too late to prevent other side-effects: The
    order in which the same event is handled for the folder tree objects is from leaf to
    root. Even if we could do something about unindexing, we cannot know what other
    handlers might be registered. So we do the robust thing and prevent a mirror from
    being removed as long as it is attached to a master folder.

    """
    if IPloneSiteRoot.providedBy(event.object):
        return

    # XXX Also abort the transaction?
    if mirror.master is not None:
        raise ValueError('Cannot remove a mirror that is still attached to a master.')


@implementer(IVocabularyFactory)
class CatalogVocabularyFactory:
    """Like plone.app.vocabularies.catalog.CatalogVocabularyFactory but without navroot"""

    def __call__(self, context, query=None):
        parsed = {}
        if query:
            parsed = parseQueryString(context, query['criteria'])
            if 'sort_on' in query:
                parsed['sort_on'] = query['sort_on']
            if 'sort_order' in query:
                parsed['sort_order'] = str(query['sort_order'])

        return CatalogVocabulary.fromItems(parsed, context)


def bare_uuid(obj):
    return getattr(obj, ATTRIBUTE_NAME, '')


def uuid_at_mirror(obj, mirror):
    """Assign mirrored objects a UUID in the context of their mirror (or master).

    It is of the form {bare-UUID}@{mirror-UUID} which is orthogonal to the UUID pattern
    used by plone.app.multlingual's shared Language Independent Folder. This makes
    mirrored content look like normal to the language redirect
    (p.a.m.browser.helper_views.universal_link).

    """
    if not IMirror.providedBy(mirror):
        return bare_uuid(obj)

    # Use context's bare UUID so that if context comes from inside a
    # plone.app.multilingual LIF, the resulting UUID doesn't look to the language
    # redirect like a strange language variant of the non-mirrored item, making a
    # language switch escape from the mirror. The mirror's UUID, sitting at the end of
    # the combination, will work fine wit the redirect.
    obj_uuid = bare_uuid(obj)

    sm = getSiteManager()
    uuid = sm.adapters.lookup((IAttributeUUID,), IUUID)
    mirror_uuid = uuid(aq_base(mirror)) or ''

    return f'{obj_uuid}@{mirror_uuid}'


# We'd really like to adapt IAttributeUUID here, but we cannot since
# plone.app.multilingual already overrides such an adapter and ZCA configuration won't
# let us compete with the override anyway. Also, we want to reuse it for non-mirrored
# content.
#
# So instead, we go for dexterity content which happens to provide IAttributeUUID.
# Adapting the DexterityContent type directly doesn't work either, so even though
# IDexterityContent doesn't extend IAttributeUUID, we still adapt it and check for
# IAttributeUUID manually. This would leave content that provides IDexterityContent but
# not IAttributeUUID without a UUID.
@implementer(IUUID)
@adapter(IDexterityContent)
def attributeUUID(context):
    if not IAttributeUUID.providedBy(context):
        raise TypeError(f'Cannot determine a UUID for {context}.')
    if mirror := mirror_info(context).mirror:
        return uuid_at_mirror(context, mirror)
    else:
        sm = getSiteManager()
        uuid = sm.adapters.lookup((IAttributeUUID,), IUUID)
        return uuid(context)


@indexer(IDexterityTranslatable)
def itgIndexer(obj):
    return None if mirror_info(obj).mirror else ITG(obj, None)


@indexer(IDexterityTranslatable)
def LanguageIndexer(obj, **kw):
    return None if mirror_info(obj).mirror else ILanguage(obj).get_language()


# modelled after plone/app/multilingual/subscriber.py


def reindex(obj, event):
    """Re-index mirrored folder content for all mirrors and master

    Mirror folder content objects are indexed once for each mirror and the
    master with different, mirror-specific UUID for each. When ever a mirrored
    folder content object is modified in some mirror or master, it must be
    re-indexed for all the other mirrors and master as well.

    """
    if IObjectRemovedEvent.providedBy(event):
        return

    info = mirror_info(obj)
    if info is NOT_MIRRORED:
        return

    # We try to access newly indexed objects via the parent in order to avoid
    # computing partial paths and starting traversal from master or mirrors.
    # However, the parent being a mirror is an exception in that mirrors' uuid
    # doesn't follow the pattern for uuids of mirrored objects.
    parent = aq_parent(obj)
    master_id = IUUID(info.master)
    if IMirror.providedBy(parent) or IUUID(parent) == master_id:
        parent_ids = [master_id] + info.mirror_ids
    else:
        parent_master_id = bare_uuid(parent)
        if not parent_master_id:
            return
        parent_ids = [parent_master_id] + [
            f'{parent_master_id}@{mirror_id}' for mirror_id in info.mirror_ids
        ]

    cat = api.portal.get_tool('portal_catalog')
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

    """
    if IObjectWillBeAddedEvent.providedBy(event):
        return
    if IPloneSiteRoot.providedBy(event.object):
        return

    info = mirror_info(obj)
    if info is NOT_MIRRORED:
        return

    uuid = IUUID(obj).split('@')[0]
    uuids = [uuid] + [f'{uuid}@{mirror_id}' for mirror_id in info.mirror_ids]

    cat = api.portal.get_tool('portal_catalog')
    for uuid in uuids:
        brains = cat.unrestrictedSearchResults(UID=uuid)
        for brain in brains:
            brain.getObject().unindexObject()


def mirror_info(obj):
    if not ICollectiveMirrorLayer.providedBy(getRequest()):
        return NOT_MIRRORED

    for element in aq_chain(obj)[1:]:
        if ISiteRoot.providedBy(element):
            break
        if mirror_ids := getattr(element, MIRRORS_ATTR, ()):
            if IMirror.providedBy(element):
                master, mirror = element.master, aq_base(element)
            else:
                master, mirror = element, None
            return MirrorInfo(aq_base(master), mirror, mirror_ids)

    return NOT_MIRRORED


def placeless_mirror_info(obj):
    """Retrieve mirror info for objects without a useful parent chain.

    Objects may not come with a useful parent chain to walk in search of a mirror, e.g.
    if they are retrieved by IntId, such as when resolving a relation. We look them up
    in the catalog by their UUID, which would find them as located in the master tree.

    """
    if (info := mirror_info(obj)) is not NOT_MIRRORED:
        return info

    cat = api.portal.get_tool('portal_catalog')
    return (
        mirror_info(brains[0].getObject())
        if (brains := cat.unrestrictedSearchResults(UID=IUUID(obj)))
        else NOT_MIRRORED
    )


def get_object_in_tree(obj, target):
    """Look up an object within the content tree of a particular mirror (or master).

    Target may either be a mirror or a mirrored folder (master).

    Object may be any content object. If part of the content tree referred to by the
    requested target, it is returned as contained within the corresponding location. The
    object may be passed as seen at any of its mirror (or master) locations.

    If the object isn't part of the tree, LocationError is raised. If it looks like the
    object is part of the tree but isn't found in the catalog as associated with this
    target, IndexError is raised.

    """
    if not getattr(target, MIRRORS_ATTR, None):
        raise ValueError(f'{target} is neither a mirror nor a mirrored folder.')

    info = placeless_mirror_info(obj)
    if not (
        aq_base(info.master) is aq_base(target)
        or info.mirror_ids and IUUID(target) in info.mirror_ids
    ):
        raise LocationError(f'{obj} is not located in the content tree of {target}.')

    return _get_object_in_tree(obj, target)


def get_object_for_language(obj, language):
    """Look up an object within the content tree of a mirror (or master), by language.

    Language may either be a language code or None. None means the master folder
    associated with the object, and a language means that mirror of the object's master
    folder which resides inside the corresponding Language Root Folder. This implies
    that the content tree in question be mirrored only once per language.

    Object may be any content object. If it is part of a mirrored content tree, it is
    returned as contained within the location at the requested language. The object may
    be passed as seen at any of its mirror (or master) locations.

    If the object isn't part of a mirrored tree, LocationError is raised. If it looks
    like the object is part of a tree but isn't found in the catalog as associated with
    the target, IndexError is raised.

    """
    info = placeless_mirror_info(obj)
    if info is NOT_MIRRORED:
        raise LocationError(f'{obj} is not located in any mirrored content tree.')

    if language is None:
        return _get_object_in_tree(obj, info.master)

    cat = api.portal.get_tool('portal_catalog')
    candidates = [
        brain
        for mirror_id in info.mirror_ids
        if (brains := cat.unrestrictedSearchResults(UID=mirror_id))
        and (brain := brains[0]).Language == language
    ]

    if not candidates:
        raise IndexError(
            f'Mirror in language {language} containing {obj} not found in catalog.'
        )
    if len(candidates) > 1:
        raise IndexError(
            f'Mirror in language {language} containing {obj} not unique in catalog.'
        )
    return _get_object_in_tree(obj, candidates[0].getObject())


def _get_object_in_tree(obj, target):
    cat = api.portal.get_tool('portal_catalog')
    obj_uuid = uuid_at_mirror(obj, target)
    brains = cat.unrestrictedSearchResults(UID=obj_uuid)
    if len(brains) != 1:
        raise IndexError(f'{obj} at {target} not found in catalog.')

    target_obj = brains[0].getObject()
    return target_obj


# Known issues:
#
# * When nested mirror content is added or moved, this
#   content isn't properly indexed. This is because the events handled by the reindex
#   handler are called by the location machinery in the order from leaves to root, which
#   breaks the assumption that we can locate content to be indexed by retrieving the
#   parent from the catalog. A work-around is rebuilding the catalog.
#
# * While we need to allow unsetting (and thus, also setting) the master of an existing
#   mirror (because we could not delete an attached mirror without collateral damage, in
#   a robust way), setting a new master doesn't get the contents indexed in the context
#   of the mirror yet.
#
# * Mirrors don't interact with multilingual content in a defined way yet. The reason is
#   that plone.app.multilingual relies on there being only one object in the catalog for
#   each pair of translation group and language. We currently work around this issue by
#   indexing any mirror content without translation group or language (hence the
#   overwritten itgIndexer and LanguageIndexer). If we ever need to be able to link
#   multilingual content objects to specific mirrors of their translations, we need to
#   rethink this.
#
# * Deleting a folder while it has mirrors isn't handled yet. Plone just warns about
#   breaking references but we might want to offer some better UI to remove all mirrors
#   along with the folder, or to replace one of the mirrors with a new master folder.
#
# * A mirror still attached to a master cannot be deleted, which is good, but we don't
#   issue a useful error message to the user yet.
#
# * The way we currently register the IUUID adapter would leave content that provides
#   IDexterityContent but not IAttributeUUID without a UUID. Are there any such types?
#
# * The number of catalog queries to find objects in a specified mirror might
#   be optimised.
