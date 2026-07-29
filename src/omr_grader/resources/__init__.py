"""Korean user-facing resource catalog."""

from .messages import MESSAGE_CATALOG, get_message, missing_message_keys, validate_message_catalog

__all__ = [
    "MESSAGE_CATALOG",
    "get_message",
    "missing_message_keys",
    "validate_message_catalog",
]
