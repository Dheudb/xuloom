"""Compatibility alias: the media tool now ships with the bundled ``media`` plugin (:mod:`xuloom.media.tool`)."""
import sys

from xuloom.media import tool as _media_tool

sys.modules[__name__] = _media_tool
