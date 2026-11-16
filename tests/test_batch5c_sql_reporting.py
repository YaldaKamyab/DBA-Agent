"""Batch 5C: generated / statically checked / executed / unverified are reported separately, per DBMS. Offline.

SQLite is really executed (in-memory). PostgreSQL, MySQL/MariaDB and SQL Server use mocked connections only: a mocked
"success" proves the reporting logic, NOT compatibility with a real server."""
import copy
import unittest
from unittest import mock

from dba_agent import seed_data as sd, sql_validate as sv
from dba_agent.stages import sql_seed
from tests.test_stage6b_seed import (LIBRARY, LITE, MS, MY, PG, FakeConn, SeedLLM, SeedStageBase, build, clinic_data, library,
                                     run_sqlite)

NAMES = {PG: "PostgreSQL", MY: "MySQL/MariaDB", MS: "SQL Server", LITE: "SQLite"}
SECRET = "S3cr3t-pw!"


def server_case(dialect):
    model = build(library(dialect))
    records = sd.validate_records(model, copy.deepcopy(LIBRARY), 3).records
    return model, records, "CREATE TABLE x (id int);", sd.render_seed_sql(model, records, "f")


def assert_not_verified(tc, rep, dialect, kind):
    tc.assertEqual(rep["selected_dbms"], dialect)
    tc.assertIsNone(rep["executed_dbms"])
    tc.assertEqual((rep["static_validation"], rep["execution"], rep["unavailable_kind"], rep["verified"]),
                   ("passed", "unavailable", kind, False))
    tc.assertEqual(rep["level"], "unverified")
    tc.assertTrue(rep["completes_stage"])                      # generation deliverable is not blocked ...
    tc.assertNotIn("executed successfully", rep["summary"])    # ... but never described as executed/verified
    tc.assertIn("NOT executed", rep["summary"])
    tc.assertIn(NAMES[dialect], rep["summary"])


class SqliteExecutionReporting(unittest.TestCase):
    def test_successful_schema_and_seed_execution(self):
        _, _, _, _, rep = run_sqlite(library(), LIBRARY)
        self.assertEqual((rep["level"], rep["execution"], rep["selected_dbms"], rep["executed_dbms"], rep["verified"]),
                         ("executed", "executed_ok", LITE, LITE, True))
        self.assertEqual(rep["static_validation"], "passed")
        self.assertIn("executed successfully on SQLite", rep["summary"])
        names = [c["name"] for c in rep["checks"]]
        self.assertIn("schema.sql executes without errors", names)
        self.assertIn("seed.sql executes without errors after schema.sql", names)

    def test_actual_sql_failure_is_execution_failed_not_unavailable(self):
        model = build(library())
        records = sd.validate_records(model, copy.deepcopy(LIBRARY), 3).records
        seed = sd.render_seed_sql(model, records, "f")
        rep = sv.validate(model, records, "CREATE TABLE categories (oops;", seed)
        self.assertEqual((rep["level"], rep["execution"], rep["verified"], rep["completes_stage"]),
                         ("failed", "execution_failed", False, False))
        self.assertEqual(rep["executed_dbms"], LITE)
        self.assertTrue(any("schema.sql" in e for e in rep["errors"]))
        self.assertIn("FAILED", rep["summary"])


class ServerUnavailable(unittest.TestCase):
    def test_no_server_configured_for_each_non_sqlite_dbms(self):
        for dialect in (PG, MY, MS):
            with self.subTest(dialect):
                rep = sv.validate(*server_case(dialect)[:1], *server_case(dialect)[1:])
                assert_not_verified(self, rep, dialect, "no_server_configured")

    def test_missing_driver_is_not_an_sql_failure(self):
        for dialect in (PG, MY, MS):
            with self.subTest(dialect):
                m, r, sch, seed = server_case(dialect)
                rep = sv.validate(m, r, sch, seed, {dialect: {"dsn": "x"}}, mock.Mock(side_effect=ImportError("No module named drv")))
                assert_not_verified(self, rep, dialect, "driver_missing")
                self.assertIn("driver not installed", rep["reason"])

    def test_connection_refused_is_unavailable_and_credentials_are_hidden(self):
        for dialect in (PG, MY, MS):
            with self.subTest(dialect):
                m, r, sch, seed = server_case(dialect)
                err = OSError(f"connection refused: dsn=postgresql://app:{SECRET}@db:5432/x password={SECRET} PWD={SECRET}")
                rep = sv.validate(m, r, sch, seed, {dialect: {"dsn": "x", "password": SECRET}}, mock.Mock(side_effect=err))
                assert_not_verified(self, rep, dialect, "connection_failed")
                self.assertNotIn(SECRET, rep["reason"] + rep["summary"])
                self.assertIn("cannot connect", rep["reason"])
                self.assertIn("OSError", rep["reason"])                 # useful error type is kept

    def test_incomplete_settings_and_non_empty_database(self):
        m, r, sch, seed = server_case(PG)
        rep = sv.validate(m, r, sch, seed, {PG: {"host": "h"}}, mock.Mock(side_effect=KeyError("dsn")))
        assert_not_verified(self, rep, PG, "config_incomplete")
        conn = FakeConn(existing=3)
        rep = sv.validate(m, r, sch, seed, {PG: {"dsn": "x"}}, lambda d, s: conn)
        assert_not_verified(self, rep, PG, "scratch_db_not_empty")
        self.assertEqual(len(conn.log), 1)                               # only the inspection query ran

    def test_server_that_cannot_be_inspected_is_unavailable_not_a_crash(self):
        m, r, sch, seed = server_case(MS)
        conn = FakeConn(fail_on="INFORMATION_SCHEMA")
        rep = sv.validate(m, r, sch, seed, {MS: {"connection_string": "x"}}, lambda d, s: conn)
        assert_not_verified(self, rep, MS, "server_check_failed")
        self.assertTrue(conn.closed)


class MockedServerExecution(unittest.TestCase):
    def test_successful_mocked_validation_reports_only_the_selected_dbms(self):
        for dialect in (PG, MY, MS):
            with self.subTest(dialect):
                m, r, sch, seed = server_case(dialect)
                seen = []
                conn = FakeConn(rows=len(next(iter(r.values()))))
                rep = sv.validate(m, r, sch, seed, {dialect: {"x": 1}}, lambda d, s: seen.append(d) or conn)
                self.assertEqual(seen, [dialect])                         # the selected DBMS, no substitution
                self.assertEqual((rep["selected_dbms"], rep["executed_dbms"], rep["execution"]),
                                 (dialect, dialect, "execution_failed" if rep["errors"] else "executed_ok"))
                self.assertIn(NAMES[dialect], rep["executed_on"])
                self.assertNotIn("SQLite", rep["executed_on"])

    def test_clean_mocked_run_is_executed_ok(self):
        m, r, sch, seed = server_case(PG)
        conn = FakeConn()
        counts = iter([0] + [len(r[t]) for t in m.tables])               # empty scratch DB, then expected row counts
        conn.cursor = lambda: type("C", (), {"execute": lambda s, q: conn.log.append(q),
                                             "fetchone": lambda s: (next(counts),)})()
        rep = sv.validate(m, r, sch, seed, {PG: {"dsn": "x"}}, lambda d, s: conn)
        self.assertEqual((rep["level"], rep["execution"], rep["verified"], rep["executed_dbms"]), ("executed", "executed_ok", True, PG))

    def test_failed_mocked_validation_hides_password_and_keeps_detail(self):
        m, r, sch, seed = server_case(MY)

        class Boom(FakeConn):
            def cursor(self_):
                c = FakeConn.cursor(self_)
                orig = c.execute

                def execute(sql):
                    if "CREATE TABLE" in sql:
                        raise RuntimeError(f"(1064) You have an error in your SQL syntax; password={SECRET}")
                    return orig(sql)
                c.execute = execute
                return c
        rep = sv.validate(m, r, sch, seed, {MY: {"password": SECRET}}, lambda d, s: Boom())
        self.assertEqual((rep["level"], rep["execution"], rep["verified"], rep["completes_stage"]),
                         ("failed", "execution_failed", False, False))
        text = " ".join(rep["errors"])
        self.assertIn("1064", text); self.assertNotIn(SECRET, text)

    def test_static_failure_means_not_attempted(self):
        m, r, sch, seed = server_case(PG)
        rep = sv.validate(m, r, sch, "-- empty", {PG: {"dsn": "x"}}, lambda d, s: self.fail("must not connect"))
        self.assertEqual((rep["static_validation"], rep["execution"], rep["level"], rep["completes_stage"]),
                         ("failed", "not_attempted", "failed", False))

    def test_no_dbms_substitution(self):
        with mock.patch.object(sv, "_validate_sqlite") as lite:
            for dialect in (PG, MY, MS):
                rep = sv.validate(*server_case(dialect))
                self.assertEqual(rep["selected_dbms"], dialect)
                self.assertNotEqual(rep["execution"], "executed_ok")
            lite.assert_not_called()
        # a settings entry for another DBMS is never used
        m, r, sch, seed = server_case(PG)
        rep = sv.validate(m, r, sch, seed, {MY: {"user": "u"}}, lambda d, s: self.fail("must not connect"))
        assert_not_verified(self, rep, PG, "no_server_configured")


class StageAndCliReporting(SeedStageBase):
    def test_offline_generation_without_server_is_complete_but_unverified(self):
        self.prepare()                                                   # default design: PostgreSQL
        res, _ = self.seed(clinic_data())
        rep = res.report
        self.assertTrue(self.path().is_file())                           # SQL generated offline
        self.assertEqual((rep["selected_dbms"], rep["executed_dbms"], rep["static_validation"], rep["execution"], rep["verified"]),
                         (PG, None, "passed", "unavailable", False))
        st = self.state()["sql_stage"]
        self.assertEqual(st["status"], "complete")
        self.assertIn("NOT executed", st["note"]); self.assertIn("not verified", st["note"])
        sm = st["seed"]["validation"]["status_summary"]
        self.assertEqual((sm["selected_dbms"], sm["executed_dbms"], sm["execution"]), (PG, None, "unavailable"))
        self.assertIn("not executed", sm["sql_execution"])

    def test_cli_messages_do_not_contradict(self):
        self.prepare()
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 0, text)
        self.assertIn("Selected DBMS: PostgreSQL", text)
        self.assertIn("SQL generation:    done", text)
        self.assertIn("Static checks:     passed", text)
        self.assertIn("NOT EXECUTED on PostgreSQL", text)
        self.assertIn("Verified on PostgreSQL: NO", text)
        self.assertIn("NOT executed or verified on PostgreSQL", text)
        self.assertIn("SQL generation only - not execution-verified", text)
        for bad in ("EXECUTED on SQLite", "executed successfully", "Verified on PostgreSQL: yes", "Verified on PostgreSQL: yes"):
            self.assertNotIn(bad, text)
        code, text = self.run_cli("seed", ["--status"])
        self.assertIn("NOT executed on the selected DBMS", text)

    def test_cli_sqlite_reports_real_execution_and_never_claims_other_dbms(self):
        self.prepare(sqlite=True)
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 0, text)
        self.assertIn("Selected DBMS: SQLite", text)
        self.assertIn("Verified on SQLite: yes", text)
        self.assertIn("executed on: SQLite", text)
        self.assertNotIn("PostgreSQL", text)
        code, text = self.run_cli("seed", ["--status"])
        self.assertIn("executed on sqlite", text)

    def test_cli_real_sqlite_failure_is_reported_as_failure(self):
        self.prepare(sqlite=True)
        bad = {"dbms": "sqlite", "level": "failed", "ok": False, "completes_stage": False, "executed_on": "SQLite 3",
               "checks": [], "errors": ["schema.sql executes without errors: statement 1 failed: boom"], "warnings": [],
               "row_counts": {}, "reason": "", "selected_dbms": "sqlite", "executed_dbms": "sqlite",
               "static_validation": "passed", "execution": "execution_failed", "unavailable_kind": None, "verified": False,
               "summary": "x"}
        with mock.patch.object(sql_seed.sql_validate, "validate", return_value=bad):
            code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 1)
        self.assertIn("FAILED on SQLite 3", text); self.assertIn("boom", text)
        self.assertIn("Verified on SQLite: NO", text)
        self.assertIn("Stage 6 is NOT complete", text); self.assertNotIn("Stage 6 is COMPLETE", text)
        self.assertNotIn("sql", self.state()["completed_stages"])


if __name__ == "__main__":
    unittest.main()
