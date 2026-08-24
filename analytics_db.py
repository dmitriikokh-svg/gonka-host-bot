"""Safe read-only access helpers for the Gonka Analytics PostgreSQL DB."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # Keep imports/test discovery usable before requirements are installed.
    import psycopg
except ImportError:  # pragma: no cover - exercised through the guarded branch
    psycopg = None


ERROR_CATEGORIES = {
    "configuration_error",
    "tunnel_unavailable",
    "authentication_failed",
    "query_timeout",
    "permission_denied",
    "invalid_response",
    "database_unavailable",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15432
DEFAULT_DATABASE = "gonka_analytics"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 30
APPLICATION_NAME = "gonka-host-bot"


class AnalyticsDbError(RuntimeError):
    """A credential-safe DB failure exposed only as a stable category."""

    def __init__(self, category: str):
        if category not in ERROR_CATEGORIES:
            category = "database_unavailable"
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, repr=False)
class AnalyticsDbSettings:
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout_seconds: int
    statement_timeout_seconds: int

    @property
    def statement_timeout_ms(self) -> int:
        return self.statement_timeout_seconds * 1000

    def __repr__(self) -> str:
        return (
            "AnalyticsDbSettings("
            f"host={self.host!r}, port={self.port!r}, "
            f"database={self.database!r}, user={self.user!r}, "
            "password=<redacted>, "
            f"connect_timeout_seconds={self.connect_timeout_seconds!r}, "
            f"statement_timeout_seconds={self.statement_timeout_seconds!r})"
        )


def _required_text(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsDbError("configuration_error")
    return value.strip()


def _text_with_default(
    environment: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    value = environment.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsDbError("configuration_error")
    return value.strip()


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int | None = None,
) -> int:
    raw = environment.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AnalyticsDbError("configuration_error") from None
    if value < 1 or (maximum is not None and value > maximum):
        raise AnalyticsDbError("configuration_error")
    return value


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> AnalyticsDbSettings:
    environment = os.environ if environ is None else environ
    return AnalyticsDbSettings(
        host=_text_with_default(
            environment,
            "GONKA_ANALYTICS_DB_HOST",
            DEFAULT_HOST,
        ),
        port=_positive_int(
            environment,
            "GONKA_ANALYTICS_DB_PORT",
            DEFAULT_PORT,
            maximum=65535,
        ),
        database=_text_with_default(
            environment,
            "GONKA_ANALYTICS_DB_NAME",
            DEFAULT_DATABASE,
        ),
        user=_required_text(environment, "GONKA_ANALYTICS_DB_USER"),
        password=_required_text(environment, "GONKA_ANALYTICS_DB_PASSWORD"),
        connect_timeout_seconds=_positive_int(
            environment,
            "GONKA_ANALYTICS_DB_CONNECT_TIMEOUT_SECONDS",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        ),
        statement_timeout_seconds=_positive_int(
            environment,
            "GONKA_ANALYTICS_DB_STATEMENT_TIMEOUT_SECONDS",
            DEFAULT_STATEMENT_TIMEOUT_SECONDS,
            maximum=2_147_483,
        ),
    )


def categorize_database_error(exc: Exception) -> str:
    if isinstance(exc, AnalyticsDbError):
        return exc.category

    sqlstate = getattr(exc, "sqlstate", None)
    message = str(exc).lower()
    if sqlstate in {"28000", "28P01"} or "password authentication failed" in message:
        return "authentication_failed"
    if sqlstate == "57014" or "statement timeout" in message:
        return "query_timeout"
    if sqlstate == "42501" or "permission denied" in message:
        return "permission_denied"
    if isinstance(exc, (ConnectionRefusedError, TimeoutError)) or any(
        marker in message
        for marker in (
            "connection refused",
            "could not connect to server",
            "no route to host",
            "connection timed out",
            "connection timeout expired",
            "timeout expired",
            "could not translate host name",
        )
    ):
        return "tunnel_unavailable"
    return "database_unavailable"


def connect_read_only(
    settings: AnalyticsDbSettings,
    *,
    connector: Callable[..., Any] | None = None,
) -> Any:
    connect = connector or (psycopg.connect if psycopg is not None else None)
    if connect is None:
        raise AnalyticsDbError("configuration_error")
    try:
        return connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            connect_timeout=settings.connect_timeout_seconds,
            application_name=APPLICATION_NAME,
            options=(
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={settings.statement_timeout_ms}"
            ),
        )
    except AnalyticsDbError:
        raise
    except Exception as exc:  # noqa: BLE001 - expose only a safe category
        raise AnalyticsDbError(categorize_database_error(exc)) from None


@contextmanager
def analytics_connection(
    settings: AnalyticsDbSettings | None = None,
    *,
    connector: Callable[..., Any] | None = None,
) -> Iterator[Any]:
    connection = connect_read_only(settings or load_settings(), connector=connector)
    try:
        yield connection
    except AnalyticsDbError:
        raise
    except Exception as exc:  # noqa: BLE001 - never expose raw DB exceptions
        raise AnalyticsDbError(categorize_database_error(exc)) from None
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - close must not expose credentials
            pass


FORBIDDEN_SQL = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|"
    r"REVOKE|COPY|CALL|DO|VACUUM|ANALYZE|REFRESH|REINDEX|CLUSTER|COMMENT|"
    r"SET|RESET|LOCK|LISTEN|NOTIFY|UNLISTEN|DISCARD|TEMP|TEMPORARY|INTO)\b",
    re.IGNORECASE,
)
LOCKING_SELECT = re.compile(
    r"\bFOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b",
    re.IGNORECASE,
)


def validate_select_sql(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise AnalyticsDbError("configuration_error")
    statement = sql.strip()
    if "\x00" in statement or "--" in statement or "/*" in statement or "*/" in statement:
        raise AnalyticsDbError("configuration_error")
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if ";" in statement or not re.match(r"^SELECT\b", statement, re.IGNORECASE):
        raise AnalyticsDbError("configuration_error")
    if FORBIDDEN_SQL.search(statement) or LOCKING_SELECT.search(statement):
        raise AnalyticsDbError("configuration_error")
    return statement


def _column_name(description: Any) -> str | None:
    value = getattr(description, "name", None)
    if value is None and isinstance(description, Sequence) and description:
        value = description[0]
    return value if isinstance(value, str) and value else None


def execute_select(
    connection: Any,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
) -> list[dict[str, Any]]:
    statement = validate_select_sql(sql)
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            description = cursor.description
            if not description:
                raise AnalyticsDbError("invalid_response")
            columns = [_column_name(item) for item in description]
            if any(name is None for name in columns) or len(set(columns)) != len(columns):
                raise AnalyticsDbError("invalid_response")
            rows = cursor.fetchall()
    except AnalyticsDbError:
        raise
    except Exception as exc:  # noqa: BLE001 - expose only a safe category
        raise AnalyticsDbError(categorize_database_error(exc)) from None

    if not isinstance(rows, list):
        raise AnalyticsDbError("invalid_response")
    result: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            if any(name not in row for name in columns):
                raise AnalyticsDbError("invalid_response")
            result.append({name: row[name] for name in columns})
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            if len(row) != len(columns):
                raise AnalyticsDbError("invalid_response")
            result.append(dict(zip(columns, row, strict=True)))
        else:
            raise AnalyticsDbError("invalid_response")
    return result
