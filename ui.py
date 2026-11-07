import inspect
import logging
import random

from pyrogram.types import InlineKeyboardButton as _B
from pyrogram.types import InlineKeyboardMarkup as KB  # noqa: F401  (re-exported)

try:
    from pyrogram.enums import ButtonStyle
except Exception:  # library without button styles
    ButtonStyle = None

log = logging.getLogger("ui")
_STYLES = ("PRIMARY", "SUCCESS", "DANGER")  # blue, green, red

try:
    COLORS = "style" in inspect.signature(_B.__init__).parameters
except Exception:
    COLORS = False


def _style():
    name = random.choice(_STYLES)
    if ButtonStyle is not None:
        return getattr(ButtonStyle, name, name.lower())
    return name.lower()


def B(text, callback_data=None, url=None):
    """InlineKeyboardButton with a random colour on every call (falls back to plain if unsupported)."""
    kw = {}
    if callback_data is not None:
        kw["callback_data"] = callback_data
    if url is not None:
        kw["url"] = url
    if COLORS:
        try:
            return _B(text, style=_style(), **kw)
        except Exception as e:
            log.warning("button style failed: %s", e)
    return _B(text, **kw)
