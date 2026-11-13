"""Data nodes: shape lists and work with dates without writing code.

Field paths use the same syntax as templates without braces, e.g. ``price`` or ``customer.email``.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.core.errors import NodeError
from app.engine.expressions import resolve_path
from app.engine.registry import NodeConfig, NodeContext, NodeResult, node

MAX_ITEMS = 10_000


def _items(value: Any) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise NodeError("'items' did not resolve to a list", code="invalid_config")
    if len(value) > MAX_ITEMS:
        raise NodeError(f"At most {MAX_ITEMS} items can be processed in one step", code="too_many_items")
    return value


def _get(item: Any, path: str) -> Any:
    return resolve_path(item, path) if path else item


def _sort_key(value: Any) -> tuple:
    """Mixed types sort predictably: missing values last, numbers before text."""
    if value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value).lower())


# ----------------------------------------------------------------------------- lists


class SortConfig(NodeConfig):
    items: Any = Field(..., description="A list, e.g. {{ nodes.fetch.body.results }}")
    sort_by: str = Field("", description="Field to sort by (empty = the items themselves)")
    order: Literal["ascending", "descending"] = "ascending"
    offset: int = Field(0, ge=0, description="Skip this many items first")
    limit: int | None = Field(None, ge=1, le=MAX_ITEMS, description="Keep at most this many (empty = all)")


@node("data.sort", "data", "Sort and Limit", "Sort a list by a field and keep the first N items.", SortConfig,
      output_schema={"items": {"type": "array"}, "count": {"type": "integer"}}, icon="arrow-down-wide-narrow")
def sort_node(ctx: NodeContext, cfg: SortConfig) -> NodeResult:
    items = sorted(_items(cfg.items), key=lambda i: _sort_key(_get(i, cfg.sort_by)), reverse=cfg.order == "descending")
    if cfg.order == "descending":  # keep missing values last in both directions
        items = [i for i in items if _get(i, cfg.sort_by) is not None] + [i for i in items if _get(i, cfg.sort_by) is None]
    items = items[cfg.offset:]
    if cfg.limit:
        items = items[: cfg.limit]
    return NodeResult(output={"items": items, "count": len(items)})


class DedupeConfig(NodeConfig):
    items: Any = Field(..., description="A list")
    by: str = Field("", description="Field that identifies duplicates, e.g. email (empty = whole item)")
    keep: Literal["first", "last"] = "first"


@node("data.dedupe", "data", "Remove Duplicates", "Keep one item per value of a field (or per identical item).", DedupeConfig,
      output_schema={"items": {"type": "array"}, "count": {"type": "integer"}, "removed": {"type": "integer"}}, icon="copy-minus")
def dedupe_node(ctx: NodeContext, cfg: DedupeConfig) -> NodeResult:
    import json

    items = _items(cfg.items)
    seq = items if cfg.keep == "first" else list(reversed(items))
    seen, out = set(), []
    for item in seq:
        value = _get(item, cfg.by)
        key = json.dumps(value, sort_keys=True, default=str).lower() if not isinstance(value, str) else value.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    if cfg.keep == "last":
        out.reverse()
    return NodeResult(output={"items": out, "count": len(out), "removed": len(items) - len(out)})


class Metric(BaseModel):
    op: Literal["count", "sum", "avg", "min", "max", "median", "list", "first", "last"] = "count"
    field: str = Field("", description="Field to measure (not needed for count)")
    name: str = Field("", description="Name of the result (default: op_field)")


class AggregateConfig(NodeConfig):
    items: Any = Field(..., description="A list")
    group_by: str = Field("", description="Field to group by (empty = one summary for the whole list)")
    metrics: list[Metric] = Field(default_factory=lambda: [Metric(op="count")], min_length=1, max_length=20)


def _measure(rows: list, m: Metric) -> Any:
    if m.op == "count":
        return len(rows) if not m.field else sum(1 for r in rows if _get(r, m.field) not in (None, ""))
    values = [_get(r, m.field) for r in rows]
    if m.op == "list":
        return values
    if m.op == "first":
        return values[0] if values else None
    if m.op == "last":
        return values[-1] if values else None
    nums = []
    for v in values:
        if isinstance(v, bool) or v in (None, ""):
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            raise NodeError(f"'{m.field}' has a value that is not a number: {str(v)[:40]}", code="invalid_data") from None
    if not nums:
        return None
    result = {"sum": sum, "avg": statistics.fmean, "min": min, "max": max, "median": statistics.median}[m.op](nums)
    return int(result) if float(result).is_integer() and m.op in ("sum", "min", "max") else round(result, 6)


@node("data.aggregate", "data", "Summarize", "Count, total, average, min/max - for a whole list or per group.", AggregateConfig,
      output_schema={"groups": {"type": "array"}, "summary": {"type": "object"}}, icon="sigma")
def aggregate_node(ctx: NodeContext, cfg: AggregateConfig) -> NodeResult:
    items = _items(cfg.items)
    names = [m.name or (m.op if not m.field else f"{m.op}_{m.field.replace('.', '_')}") for m in cfg.metrics]
    if not cfg.group_by:
        return NodeResult(output={"summary": {n: _measure(items, m) for n, m in zip(names, cfg.metrics)}, "groups": []})
    groups: dict[str, list] = {}
    keys: dict[str, Any] = {}
    for item in items:
        key = _get(item, cfg.group_by)
        k = str(key)
        groups.setdefault(k, []).append(item)
        keys[k] = key
    out = [{"key": keys[k], **{n: _measure(rows, m) for n, m in zip(names, cfg.metrics)}} for k, rows in groups.items()]
    return NodeResult(output={"groups": out, "summary": {"groups": len(out), "items": len(items)}})


# ----------------------------------------------------------------------------- dates


class DateConfig(NodeConfig):
    operation: Literal["now", "parse", "add", "subtract", "difference", "start_of", "format"] = "now"
    value: str = Field("", description="Date/time to work on (ISO 8601, a unix timestamp, or e.g. 2026-11-13 09:30). Empty = now")
    amount: float = Field(0, description="For add/subtract")
    unit: Literal["minutes", "hours", "days", "weeks", "months"] = "days"
    other: str = Field("", description="For difference: the second date (empty = now)")
    period: Literal["day", "week", "month", "year"] = Field("day", description="For start_of")
    timezone: str = Field("UTC", description="IANA zone for parsing, formatting and day boundaries, e.g. Europe/Sofia")
    format: str = Field("%Y-%m-%d %H:%M", description="For format: strftime pattern, e.g. %d.%m.%Y")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise NodeError(f"Unknown time zone '{name}'", code="invalid_config") from None


def _parse(value: Any, zone: ZoneInfo) -> datetime:
    if value in (None, ""):
        return datetime.now(timezone.utc).astimezone(zone)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").replace(".", "", 1).isdigit()):
        ts = float(value)
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, timezone.utc).astimezone(zone)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
            return (dt if dt.tzinfo else dt.replace(tzinfo=zone)).astimezone(zone)
        except ValueError:
            continue
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=zone)
        except ValueError:
            continue
    raise NodeError(f"Could not read '{text[:40]}' as a date", code="invalid_date")


def _shift(dt: datetime, amount: float, unit: str) -> datetime:
    if unit == "months":
        whole = int(amount)
        month = dt.month - 1 + whole
        year, month = dt.year + month // 12, month % 12 + 1
        import calendar

        return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))
    return dt + timedelta(**{unit: amount})


def _describe(dt: datetime) -> dict:
    return {"iso": dt.isoformat(), "date": dt.date().isoformat(), "time": dt.strftime("%H:%M"), "unix": int(dt.timestamp()),
            "weekday": dt.strftime("%A"), "utc": dt.astimezone(timezone.utc).isoformat()}


@node("data.datetime", "data", "Date & Time", "Parse, shift, compare and format dates in any time zone.", DateConfig,
      output_schema={"iso": {"type": "string"}, "date": {"type": "string"}, "time": {"type": "string"}, "formatted": {"type": "string"},
                     "unix": {"type": "integer"}, "weekday": {"type": "string"}, "difference": {"type": "object"}}, icon="calendar-clock")
def datetime_node(ctx: NodeContext, cfg: DateConfig) -> NodeResult:
    zone = _zone(cfg.timezone)
    dt = _parse(cfg.value, zone)
    if cfg.operation in ("add", "subtract"):
        dt = _shift(dt, cfg.amount if cfg.operation == "add" else -cfg.amount, cfg.unit)
    elif cfg.operation == "start_of":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if cfg.period == "week":
            dt -= timedelta(days=dt.weekday())
        elif cfg.period == "month":
            dt = dt.replace(day=1)
        elif cfg.period == "year":
            dt = dt.replace(month=1, day=1)
    out = _describe(dt)
    if cfg.operation == "difference":
        other = _parse(cfg.other, zone)
        seconds = (other - dt).total_seconds()
        out["difference"] = {"seconds": int(seconds), "minutes": round(seconds / 60, 2), "hours": round(seconds / 3600, 2),
                             "days": round(seconds / 86400, 2), "is_past": seconds > 0}
    try:
        out["formatted"] = dt.strftime(cfg.format or "%Y-%m-%d %H:%M")
    except ValueError:
        raise NodeError("Invalid date format pattern", code="invalid_config") from None
    return NodeResult(output=out)
