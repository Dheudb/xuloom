"""Optional HTTP + Server-Sent Events API (``xuloom serve``); see ``docs/server.md``."""
from xuloom.server.app import HttpError, XuloomServer

__all__ = ["XuloomServer", "HttpError"]
