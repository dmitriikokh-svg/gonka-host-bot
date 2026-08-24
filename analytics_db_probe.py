"""Credential-safe, read-only health probe for the Gonka Analytics DB."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from analytics_db import (
    AnalyticsDbError,
    AnalyticsDbSettings,
    analytics_connection,
    execute_select,
)


LATEST_BLOCK_SQL = """
SELECT max(height) AS height, max(block_time) AS block_time
FROM blocks
"""

LIVE_EPOCH_SQL = """
SELECT max(epoch_number) AS epoch_number
FROM epochs
"""

LATEST_SETTLED_EPOCH_SQL = """
SELECT max(epoch_number) AS epoch_number
FROM epochs
WHERE total_rewards_gnk > 0
"""

TABLE_EXISTS_SQL = "SELECT to_regclass(%s) AS relation"

REQUIRED_TABLES = (
    "epochs",
    "epoch_participants",
    "epoch_participant_models",
    "host_versions",
    "devshard_inferences",
    "devshard_agg_daily",
    "devshard_escrow_registry",
    "participant_geo",
)


SelectExecutor = Callable[
    [Any, str, Sequence[Any] | Mapping[str, Any]],
    list[dict[str, Any]],
]


def _one_row(rows: Any) -> dict[str, Any]:
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise AnalyticsDbError("invalid_response")
    return rows[0]


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AnalyticsDbError("invalid_response")
    return value


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise AnalyticsDbError("invalid_response") from None
    else:
        raise AnalyticsDbError("invalid_response")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticsDbError("invalid_response")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def collect_probe(
    connection: Any,
    *,
    execute: SelectExecutor = execute_select,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect the minimal indexer/read-model health snapshot."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise AnalyticsDbError("configuration_error")
    current_time = current_time.astimezone(timezone.utc)

    block = _one_row(execute(connection, LATEST_BLOCK_SQL, ()))
    latest_height = _positive_integer(block.get("height"))
    latest_block_time = _aware_datetime(block.get("block_time"))

    live = _one_row(execute(connection, LIVE_EPOCH_SQL, ()))
    live_epoch = _positive_integer(live.get("epoch_number"))

    settled = _one_row(execute(connection, LATEST_SETTLED_EPOCH_SQL, ()))
    latest_settled_epoch = _positive_integer(settled.get("epoch_number"))

    tables: dict[str, bool] = {}
    for table_name in REQUIRED_TABLES:
        table = _one_row(execute(connection, TABLE_EXISTS_SQL, (table_name,)))
        relation = table.get("relation")
        if relation is not None and not isinstance(relation, str):
            raise AnalyticsDbError("invalid_response")
        tables[table_name] = bool(relation)

    lag_seconds = max(0, int((current_time - latest_block_time).total_seconds()))
    return {
        "status": "ok",
        "latest_height": latest_height,
        "latest_block_time": _iso_utc(latest_block_time),
        "indexer_lag_seconds": lag_seconds,
        "live_epoch": live_epoch,
        "latest_settled_epoch": latest_settled_epoch,
        "tables": tables,
    }


def run_probe(
    settings: AnalyticsDbSettings | None = None,
    *,
    connector: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    with analytics_connection(settings, connector=connector) as connection:
        return collect_probe(connection, now=now)


def main() -> int:
    try:
        result = run_probe()
    except AnalyticsDbError as exc:
        result = {"status": "error", "error_category": exc.category}
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
