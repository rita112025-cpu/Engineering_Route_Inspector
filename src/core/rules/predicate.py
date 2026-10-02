"""Predicate trees (AND / OR / NOT) over entities and entity pairs.

Predicates are plain JSON data interpreted here. No eval(), no code execution.

Entity predicate leaf keys (all keys in one dict are AND-ed):
  layer_regex, layer_equals, layer_contains, entity_type, system, text_regex
Pair predicate leaf keys:
  different_layer, same_layer, different_system, same_system
Combinators (allowed in both): and: [..], or: [..], not: {..}
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from core.models.entities import GeometryEntity

ENTITY_KEYS = {"layer_regex", "layer_equals", "layer_contains", "entity_type", "system", "text_regex"}
PAIR_KEYS = {"different_layer", "same_layer", "different_system", "same_system"}
COMBINATORS = {"and", "or", "not"}
MAX_DEPTH = 16
MAX_REGEX_LEN = 200


class PredicateError(ValueError):
    pass


@lru_cache(maxsize=512)
def compile_regex(pattern: str) -> re.Pattern:
    if len(pattern) > MAX_REGEX_LEN:
        raise PredicateError(f"正規表示式太長（上限 {MAX_REGEX_LEN} 字元）")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise PredicateError(f"正規表示式格式錯誤：{pattern}（{exc}）") from exc


def validate(node: Any, kind: str = "entity", depth: int = 0) -> None:
    allowed = ENTITY_KEYS if kind == "entity" else PAIR_KEYS
    if depth > MAX_DEPTH:
        raise PredicateError("條件巢狀層數太深")
    if not isinstance(node, dict):
        raise PredicateError("條件必須是物件（dict）")
    if not node:
        raise PredicateError("條件不可為空")
    for key, val in node.items():
        if key in ("and", "or"):
            if not isinstance(val, list) or not val:
                raise PredicateError(f"'{key}' 需要非空清單")
            for child in val:
                validate(child, kind, depth + 1)
        elif key == "not":
            validate(val, kind, depth + 1)
        elif key in allowed:
            if key in ("layer_regex", "text_regex"):
                if not isinstance(val, str):
                    raise PredicateError(f"'{key}' 必須是文字")
                compile_regex(val)
            elif key in ("layer_equals", "layer_contains"):
                if not isinstance(val, str):
                    raise PredicateError(f"'{key}' 必須是文字")
            elif key in ("entity_type", "system"):
                if not isinstance(val, (str, list)) or (
                        isinstance(val, list) and (not val or not all(isinstance(v, str) for v in val))):
                    raise PredicateError(f"'{key}' 必須是文字或非空的文字清單")
            elif not isinstance(val, bool):
                raise PredicateError(f"'{key}' 必須是 true/false")
        else:
            raise PredicateError(f"不支援的條件欄位：{key}")


def _as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def match_entity(node: dict, e: GeometryEntity) -> bool:
    for key, val in node.items():
        if key == "and":
            if not all(match_entity(c, e) for c in val):
                return False
        elif key == "or":
            if not any(match_entity(c, e) for c in val):
                return False
        elif key == "not":
            if match_entity(val, e):
                return False
        elif key == "layer_regex":
            if not compile_regex(val).search(e.layer):
                return False
        elif key == "layer_equals":
            if e.layer.casefold() != val.casefold():
                return False
        elif key == "layer_contains":
            if val.casefold() not in e.layer.casefold():
                return False
        elif key == "entity_type":
            if e.entity_type.upper() not in {t.upper() for t in _as_list(val)}:
                return False
        elif key == "system":
            sys_name = e.metadata.get("system")
            if not sys_name or sys_name.casefold() not in {s.casefold() for s in _as_list(val)}:
                return False
        elif key == "text_regex":
            text = e.geometry.get("text", "") if e.kind == "text" else ""
            if not compile_regex(val).search(text):
                return False
        else:
            raise PredicateError(f"不支援的條件欄位：{key}")
    return True


def match_pair(node: dict, a: GeometryEntity, b: GeometryEntity) -> bool:
    for key, val in node.items():
        if key == "and":
            if not all(match_pair(c, a, b) for c in val):
                return False
        elif key == "or":
            if not any(match_pair(c, a, b) for c in val):
                return False
        elif key == "not":
            if match_pair(val, a, b):
                return False
        elif key in ("different_layer", "same_layer"):
            same = a.layer.casefold() == b.layer.casefold()
            want_same = (key == "same_layer") == bool(val)
            if same != want_same:
                return False
        elif key in ("different_system", "same_system"):
            sa, sb = a.metadata.get("system"), b.metadata.get("system")
            if not sa or not sb:
                return False  # unclassified: cannot assert either relation
            same = sa.casefold() == sb.casefold()
            want_same = (key == "same_system") == bool(val)
            if same != want_same:
                return False
        else:
            raise PredicateError(f"不支援的配對條件欄位：{key}")
    return True


def explicitly_mentions_text(node: dict) -> bool:
    """Whether the predicate opts in to TEXT/MTEXT entities."""
    for key, val in node.items():
        if key == "text_regex":
            return True
        if key == "entity_type" and any(t.upper() in ("TEXT", "MTEXT") for t in _as_list(val)):
            return True
        if key in ("and", "or") and any(explicitly_mentions_text(c) for c in val):
            return True
    return False


def describe(node: dict) -> str:
    """Human-readable summary of an entity predicate (Chinese)."""
    parts = []
    for key, val in node.items():
        if key == "and":
            parts.append("（" + " 且 ".join(describe(c) for c in val) + "）")
        elif key == "or":
            parts.append("（" + " 或 ".join(describe(c) for c in val) + "）")
        elif key == "not":
            parts.append("非 " + describe(val))
        elif key == "layer_regex":
            parts.append(f"圖層符合 /{val}/")
        elif key == "layer_equals":
            parts.append(f"圖層 = {val}")
        elif key == "layer_contains":
            parts.append(f"圖層包含「{val}」")
        elif key == "entity_type":
            parts.append("類型為 " + "/".join(_as_list(val)))
        elif key == "system":
            parts.append("系統為 " + "/".join(_as_list(val)))
        elif key == "text_regex":
            parts.append(f"文字符合 /{val}/")
        elif key in PAIR_KEYS:
            label = {"different_layer": "不同圖層", "same_layer": "相同圖層",
                     "different_system": "不同系統", "same_system": "相同系統"}[key]
            parts.append(label if val else "非" + label)
    return " 且 ".join(parts)
