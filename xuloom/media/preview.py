"""Compatibility alias: file previews moved to :mod:`xuloom.features.media_preview` (core)."""
import sys

from xuloom.features import media_preview as _media_preview

sys.modules[__name__] = _media_preview
