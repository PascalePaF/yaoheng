"""Stable, keyed updates for financial Treeviews.

Replacing every row on each tick causes selection loss, scroll jumps and a
blank-frame flash.  This reconciler edits only changed rows in place.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import math
from typing import Any


Row = tuple[tuple[str, ...], tuple[str, ...]]


def sort_financial_rows(
    rows: Sequence[Row], index: int, *, numeric: bool, reverse: bool,
    compact: bool = False,
) -> list[Row]:
    """Sort without treating missing quotes as zero or moving them to the top."""

    if not numeric:
        return sorted(rows, key=lambda row: row[0][index].casefold(), reverse=reverse)
    valued: list[tuple[float, Row]] = []
    missing: list[Row] = []
    for row in rows:
        value = row[0][index].replace(",", "").replace("%", "").replace("+", "")
        scale = 1.0
        if compact and value.endswith(("K", "M")):
            scale = 1_000.0 if value.endswith("K") else 1_000_000.0
            value = value[:-1]
        try:
            number = float(value) * scale
        except ValueError:
            missing.append(row)
            continue
        if math.isfinite(number):
            valued.append((number, row))
        else:
            missing.append(row)
    valued.sort(key=lambda item: item[0], reverse=reverse)
    return [row for _number, row in valued] + missing


def reconcile_rows(
    tree: Any,
    rows: Sequence[Row],
    key: Callable[[tuple[str, ...], tuple[str, ...]], str],
) -> dict[str, str]:
    """Return item-id to row-key mapping, retaining unchanged Tk item IDs."""

    row_keys = [key(values, tags) for values, tags in rows]
    if len(set(row_keys)) != len(row_keys):
        raise ValueError("duplicate table row key")

    old_items: dict[str, str] = {}
    duplicate_items: list[str] = []
    for item_id in tree.get_children():
        values = tuple(str(value) for value in tree.item(item_id, "values"))
        tags = tuple(str(tag) for tag in tree.item(item_id, "tags"))
        row_key = key(values, tags)
        if row_key in old_items:
            duplicate_items.append(item_id)
        else:
            old_items[row_key] = item_id

    item_keys: dict[str, str] = {}
    desired_order: list[str] = []
    for (values, tags), row_key in zip(rows, row_keys):
        item_id = old_items.pop(row_key, None)
        if item_id is None:
            item_id = tree.insert("", "end", values=values, tags=tags)
        else:
            if tuple(str(value) for value in tree.item(item_id, "values")) != values:
                tree.item(item_id, values=values)
            if tuple(str(tag) for tag in tree.item(item_id, "tags")) != tags:
                tree.item(item_id, tags=tags)
        item_keys[item_id] = row_key
        desired_order.append(item_id)

    stale = [*old_items.values(), *duplicate_items]
    if stale:
        tree.delete(*stale)
    if tuple(tree.get_children()) != tuple(desired_order):
        for index, item_id in enumerate(desired_order):
            if tree.index(item_id) != index:
                tree.move(item_id, "", index)
    return item_keys
