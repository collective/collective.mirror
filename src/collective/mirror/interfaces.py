"""Module where all interfaces, events and exceptions live."""

from zope.interface import Interface
from zope.publisher.interfaces.browser import IDefaultBrowserLayer


class ICollectiveMirrorLayer(IDefaultBrowserLayer):
    """Marker interface that defines a browser layer."""


class ILanguageSelectable(Interface):
    """Marker interface for non-translatables that should allow language selection.

    This makes sense in the case of mirrors that are translations of each other but
    refer to a master folder that is not itself translated.

    """
