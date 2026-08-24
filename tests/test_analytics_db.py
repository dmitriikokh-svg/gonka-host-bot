import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import analytics_db
import analytics_db_probe


ROOT = Path(__file__).resolve().parents[1]


class FakeColumn:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, columns, rows):
        self.description = [FakeColumn(name) for name in columns]
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class FakeDatabaseError(Exception):
    def __init__(self, message, sqlstate=None):
        super().__init__(message)
        self.sqlstate = sqlstate


class AnalyticsDbSettingsTests(unittest.TestCase):
    def test_required_credentials_and_safe_error(self):
        with self.assertRaises(analytics_db.AnalyticsDbError) as context:
            analytics_db.load_settings({})
        self.assertEqual(context.exception.category, "configuration_error")
        self.assertEqual(str(context.exception), "configuration_error")

    def test_defaults_and_redacted_repr(self):
        secret = "do-not-print-this-password"
        settings = analytics_db.load_settings(
            {
                "GONKA_ANALYTICS_DB_USER": "reader",
                "GONKA_ANALYTICS_DB_PASSWORD": secret,
            }
        )
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 15432)
        self.assertEqual(settings.database, "gonka_analytics")
        self.assertEqual(settings.connect_timeout_seconds, 10)
        self.assertEqual(settings.statement_timeout_seconds, 30)
        self.assertNotIn(secret, repr(settings))
        self.assertIn("password=<redacted>", repr(settings))

    def test_connector_receives_separate_credentials_and_read_only_options(self):
        settings = analytics_db.AnalyticsDbSettings(
            host="127.0.0.1",
            port=15432,
            database="gonka_analytics",
            user="reader",
            password="secret",
            connect_timeout_seconds=10,
            statement_timeout_seconds=30,
        )
        connection = FakeConnection()
        connector = mock.Mock(return_value=connection)

        self.assertIs(
            analytics_db.connect_read_only(settings, connector=connector),
            connection,
        )

        connector.assert_called_once_with(
            host="127.0.0.1",
            port=15432,
            dbname="gonka_analytics",
            user="reader",
            password="secret",
            connect_timeout=10,
            application_name="gonka-host-bot",
            options=(
                "-c default_transaction_read_only=on "
                "-c statement_timeout=30000"
            ),
        )

    def test_connection_exception_does_not_expose_connector_message(self):
        secret = "do-not-print-this-password"
        settings = analytics_db.load_settings(
            {
                "GONKA_ANALYTICS_DB_USER": "reader",
                "GONKA_ANALYTICS_DB_PASSWORD": secret,
            }
        )
        connector = mock.Mock(
            side_effect=FakeDatabaseError(
                f"password authentication failed for {secret}",
                "28P01",
            )
        )

        with self.assertRaises(analytics_db.AnalyticsDbError) as context:
            analytics_db.connect_read_only(settings, connector=connector)

        self.assertEqual(context.exception.category, "authentication_failed")
        self.assertNotIn(secret, str(context.exception))


class SelectSafetyTests(unittest.TestCase):
    def test_parameterized_select(self):
        cursor = FakeCursor(["value"], [("safe",)])
        connection = FakeConnection(cursor)

        rows = analytics_db.execute_select(
            connection,
            "SELECT value FROM sample WHERE id = %s",
            (7,),
        )

        self.assertEqual(rows, [{"value": "safe"}])
        self.assertEqual(
            cursor.executed,
            ("SELECT value FROM sample WHERE id = %s", (7,)),
        )

    def test_mutating_or_locking_sql_is_rejected(self):
        unsafe = (
            "INSERT INTO sample VALUES (1)",
            "UPDATE sample SET value = 1",
            "DELETE FROM sample",
            "DROP TABLE sample",
            "CREATE TEMP TABLE sample (id int)",
            "SELECT * INTO backup FROM sample",
            "SELECT * FROM sample FOR UPDATE",
            "SELECT 1; SELECT 2",
            "SELECT 1 -- comment",
        )
        for sql in unsafe:
            with self.subTest(sql=sql):
                with self.assertRaises(analytics_db.AnalyticsDbError) as context:
                    analytics_db.validate_select_sql(sql)
                self.assertEqual(context.exception.category, "configuration_error")

    def test_safe_error_categories(self):
        cases = (
            (FakeDatabaseError("secret", "28P01"), "authentication_failed"),
            (FakeDatabaseError("secret", "57014"), "query_timeout"),
            (FakeDatabaseError("secret", "42501"), "permission_denied"),
            (ConnectionRefusedError("secret"), "tunnel_unavailable"),
            (RuntimeError("connection timeout expired: secret"), "tunnel_unavailable"),
            (RuntimeError("server exploded: secret"), "database_unavailable"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    analytics_db.categorize_database_error(error),
                    expected,
                )


class AnalyticsDbProbeTests(unittest.TestCase):
    NOW = datetime(2026, 8, 24, 12, 0, 30, tzinfo=timezone.utc)
    BLOCK_TIME = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def successful_execute(connection, sql, params):
        del connection
        if sql == analytics_db_probe.LATEST_BLOCK_SQL:
            return [
                {
                    "height": 5_600_001,
                    "block_time": AnalyticsDbProbeTests.BLOCK_TIME,
                }
            ]
        if sql == analytics_db_probe.LIVE_EPOCH_SQL:
            return [{"epoch_number": 370}]
        if sql == analytics_db_probe.LATEST_SETTLED_EPOCH_SQL:
            return [{"epoch_number": 369}]
        if sql == analytics_db_probe.TABLE_EXISTS_SQL:
            relation = None if params == ("participant_geo",) else params[0]
            return [{"relation": relation}]
        raise AssertionError(f"unexpected SQL constant: {sql!r}")

    def test_probe_returns_only_aggregate_health_data(self):
        result = analytics_db_probe.collect_probe(
            object(),
            execute=self.successful_execute,
            now=self.NOW,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["latest_height"], 5_600_001)
        self.assertEqual(result["latest_block_time"], "2026-08-24T12:00:00Z")
        self.assertEqual(result["indexer_lag_seconds"], 30)
        self.assertEqual(result["live_epoch"], 370)
        self.assertEqual(result["latest_settled_epoch"], 369)
        self.assertFalse(result["tables"]["participant_geo"])
        self.assertEqual(set(result["tables"]), set(analytics_db_probe.REQUIRED_TABLES))
        serialized = json.dumps(result)
        self.assertNotIn("participant_address", serialized)
        self.assertNotIn("password", serialized)

    def test_null_or_malformed_core_values_are_invalid_response(self):
        invalid_values = (None, 0, True, "5600001")
        for invalid in invalid_values:
            with self.subTest(value=invalid):
                def invalid_execute(connection, sql, params):
                    result = self.successful_execute(connection, sql, params)
                    if sql == analytics_db_probe.LATEST_BLOCK_SQL:
                        result[0]["height"] = invalid
                    return result

                with self.assertRaises(analytics_db.AnalyticsDbError) as context:
                    analytics_db_probe.collect_probe(
                        object(), execute=invalid_execute, now=self.NOW
                    )
                self.assertEqual(context.exception.category, "invalid_response")

    def test_malformed_row_is_invalid_response(self):
        def invalid_execute(connection, sql, params):
            del connection, sql, params
            return []

        with self.assertRaises(analytics_db.AnalyticsDbError) as context:
            analytics_db_probe.collect_probe(
                object(), execute=invalid_execute, now=self.NOW
            )
        self.assertEqual(context.exception.category, "invalid_response")

    def test_main_emits_safe_success_json(self):
        result = analytics_db_probe.collect_probe(
            object(), execute=self.successful_execute, now=self.NOW
        )
        output = io.StringIO()
        with mock.patch.object(analytics_db_probe, "run_probe", return_value=result):
            with mock.patch("sys.stdout", output):
                exit_code = analytics_db_probe.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_main_emits_only_safe_error_category(self):
        secret = "postgresql://reader:password@example.invalid/database"
        output = io.StringIO()
        error = analytics_db.AnalyticsDbError("authentication_failed")
        with mock.patch.object(analytics_db_probe, "run_probe", side_effect=error):
            with mock.patch("sys.stdout", output):
                exit_code = analytics_db_probe.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "error", "error_category": "authentication_failed"},
        )
        self.assertNotIn(secret, output.getvalue())

    def test_probe_has_no_telegram_state_or_workflow_integration(self):
        source = (ROOT / "analytics_db_probe.py").read_text(encoding="utf-8")
        self.assertNotIn("telegram", source.lower())
        self.assertNotIn("state/", source.lower())
        workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
        references = [
            path.name
            for path in workflows
            if "analytics_db_probe" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(references, [])


if __name__ == "__main__":
    unittest.main()
