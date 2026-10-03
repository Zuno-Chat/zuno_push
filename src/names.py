from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NAME_BYTES = 64
BIDI_CONTROLS = frozenset([0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])


def is_stripped(char: str) -> bool:
    code = ord(char)
    return code <= 0x1F or 0x7F <= code <= 0x9F or code in BIDI_CONTROLS


def strip_controls(text: str) -> str:
    return "".join(char for char in text if not is_stripped(char))


def cut_utf8(text: str, limit: int = NAME_BYTES) -> str:
    return text.encode("utf-8", errors="ignore")[:limit].decode("utf-8", errors="ignore")


def blob_name(text: str) -> str:
    return cut_utf8(strip_controls(text))


def _content(event: Any) -> Mapping[str, Any]:
    content = getattr(event, "content", None)
    return content if isinstance(content, Mapping) else {}


def raw_name(event: Any, key: str) -> str | None:
    value = _content(event).get(key)
    return value if isinstance(value, str) and value else None


def member_name(event: Any) -> str:
    name = _content(event).get("displayname")
    return blob_name(name) if isinstance(name, str) else ""


def room_title(name_event: Any, alias_event: Any) -> str:
    name = _content(name_event).get("name")
    title = blob_name(name) if isinstance(name, str) else ""
    if title:
        return title
    alias = _content(alias_event).get("alias")
    return blob_name(alias) if isinstance(alias, str) else ""


def is_direct(direct: Mapping[str, Any] | None, room_id: str) -> bool:
    if not isinstance(direct, Mapping):
        return False
    for rooms in direct.values():
        if isinstance(rooms, (list, tuple)) and room_id in rooms:
            return True
    return False


def ignores(ignored: Mapping[str, Any] | None, user_id: str) -> bool:
    if not isinstance(ignored, Mapping):
        return False
    users = ignored.get("ignored_users")
    return isinstance(users, Mapping) and user_id in users
