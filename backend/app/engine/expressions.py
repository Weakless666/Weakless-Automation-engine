"""Safe templating and condition evaluation for workflow configuration.

There is no ``eval``: ``{{ ... }}`` expressions are a dotted path into the
execution scope followed by optional whitelisted filters, e.g.
``{{ trigger.body.email | lower }}`` or ``{{ nodes.lookup.items[0].id | default('n/a') }}``.

A string that consists of exactly one expression keeps the resolved value's
type (lists and objects stay lists and objects); otherwise the values are
interpolated into the string.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

_EXPR = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.DOTALL)
_SEGMENT = re.compile(r"([A-Za-z0-9_\-]+)|\[(\d+)\]|\[['\"]([^'\"]+)['\"]\]")
_FILTER = re.compile(r"^([a-z_]+)(?:\((.*)\))?$", re.DOTALL)
MAX_REGEX_LEN = 200
MAX_REGEX_INPUT = 10000


def resolve_path(scope: Any, path: str) -> Any:
    cur = scope
    pos = 0
    path = path.strip()
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = _SEGMENT.match(path, pos)
        if not m:
            return None
        key, idx, quoted = m.groups()
        pos = m.end()
        if idx is not None:
            if isinstance(cur, (list, tuple)) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return None
        else:
            k = key if key is not None else quoted
            if isinstance(cur, dict):
                cur = cur.get(k)
            elif isinstance(cur, (list, tuple)) and k is not None and k.isdigit() and int(k) < len(cur):
                cur = cur[int(k)]
            else:
                return None
        if cur is None:
            return None
    return cur


def _arg(raw: str | None) -> Any:
    if raw is None or raw.strip() == "":
        return None
    raw = raw.strip()
    if (raw[0] == raw[-1]) and raw[0] in "'\"":
        return raw[1:-1]
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _to_number(v: Any) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


FILTERS: dict[str, Callable[[Any, Any], Any]] = {
    "default": lambda v, a: a if v in (None, "") else v,
    "json": lambda v, a: json.dumps(v, default=str),
    "upper": lambda v, a: str(v).upper() if v is not None else v,
    "lower": lambda v, a: str(v).lower() if v is not None else v,
    "trim": lambda v, a: str(v).strip() if v is not None else v,
    "string": lambda v, a: "" if v is None else (v if isinstance(v, str) else json.dumps(v, default=str)),
    "number": lambda v, a: _to_number(v),
    "length": lambda v, a: len(v) if hasattr(v, "__len__") else 0,
    "first": lambda v, a: v[0] if isinstance(v, (list, tuple)) and v else None,
    "last": lambda v, a: v[-1] if isinstance(v, (list, tuple)) and v else None,
    "join": lambda v, a: (a if a is not None else ", ").join(str(x) for x in v) if isinstance(v, (list, tuple)) else v,
    "truncate": lambda v, a: str(v)[: int(a or 200)] if v is not None else v,
    "multiply": lambda v, a: _arith(v, a, lambda x, y: x * y),
    "add": lambda v, a: _arith(v, a, lambda x, y: x + y),
}


def _arith(v: Any, a: Any, op: Callable[[float, float], float]) -> Any:
    x, y = _to_number(v), _to_number(a)
    if x is None or y is None:
        return None
    r = op(x, y)
    return int(r) if float(r).is_integer() else r


def _split_pipes(expr: str) -> list[str]:
    parts, cur, quote, depth = [], "", None, 0
    for ch in expr:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


def evaluate_expression(expr: str, scope: dict) -> Any:
    parts = _split_pipes(expr)
    value = resolve_path(scope, parts[0])
    for f in parts[1:]:
        m = _FILTER.match(f)
        if not m or m.group(1) not in FILTERS:
            raise ValueError(f"Unknown template filter '{f}'")
        value = FILTERS[m.group(1)](value, _arg(m.group(2)))
    return value


def render(value: Any, scope: dict) -> Any:
    """Recursively render templates inside ``value``."""
    if isinstance(value, str):
        if "{{" not in value:
            return value
        whole = _EXPR.fullmatch(value.strip())
        if whole and value.strip().count("{{") == 1:
            return evaluate_expression(whole.group(1), scope)

        def sub(m: re.Match) -> str:
            v = evaluate_expression(m.group(1), scope)
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v, default=str)

        return _EXPR.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


def has_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


# ----------------------------------------------------------------------------- conditions

OPERATORS = (
    "equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with", "gt", "gte", "lt", "lte",
    "is_empty", "is_not_empty", "is_true", "is_false", "in", "not_in", "matches_regex",
)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _cmp(left: Any, right: Any) -> tuple[Any, Any]:
    ln, rn = _to_number(left), _to_number(right)
    if ln is not None and rn is not None and not isinstance(left, bool) and not isinstance(right, bool):
        return ln, rn
    return str(left) if left is not None else "", str(right) if right is not None else ""


def _loose_eq(left: Any, right: Any) -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return str(left).lower() == str(right).lower()
    a, b = _cmp(left, right)
    return a == b


def evaluate_condition(cond: dict) -> bool:
    """Evaluate an already-rendered condition or condition group."""
    if not cond:
        return True
    if "conditions" in cond:
        results = [evaluate_condition(c) for c in cond.get("conditions") or []]
        if not results:
            return True
        return any(results) if cond.get("combinator", "and") == "or" else all(results)
    op = cond.get("operator", "equals")
    left, right = cond.get("left"), cond.get("right")
    if op == "equals":
        return _loose_eq(left, right)
    if op == "not_equals":
        return not _loose_eq(left, right)
    if op == "is_empty":
        return _empty(left)
    if op == "is_not_empty":
        return not _empty(left)
    if op == "is_true":
        return left is True or str(left).lower() in ("true", "1", "yes")
    if op == "is_false":
        return left is False or _empty(left) or str(left).lower() in ("false", "0", "no")
    if op in ("contains", "not_contains"):
        if isinstance(left, (list, tuple)):
            found = any(_loose_eq(x, right) for x in left)
        elif isinstance(left, dict):
            found = str(right) in left
        else:
            found = str(right).lower() in str(left if left is not None else "").lower()
        return found if op == "contains" else not found
    if op in ("in", "not_in"):
        options = right if isinstance(right, (list, tuple)) else [s.strip() for s in str(right or "").split(",")]
        found = any(_loose_eq(left, o) for o in options)
        return found if op == "in" else not found
    if op == "starts_with":
        return str(left or "").lower().startswith(str(right or "").lower())
    if op == "ends_with":
        return str(left or "").lower().endswith(str(right or "").lower())
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _cmp(left, right)
        try:
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        except TypeError:
            return False
    if op == "matches_regex":
        pattern = str(right or "")
        if len(pattern) > MAX_REGEX_LEN:
            raise ValueError("Regular expression is too long")
        return re.search(pattern, str(left or "")[:MAX_REGEX_INPUT]) is not None
    raise ValueError(f"Unknown operator '{op}'")
