"""Task A regression tests (bugs 1-4)."""
import unittest

from dba_agent.errors import DBAAgentError
from dba_agent.schema_sql import check_type, POSTGRESQL, MYSQL, SQLITE, SQL_SERVER


class TypeParamTests(unittest.TestCase):
    def test_invalid_rejected(self):
        for d, t in [(POSTGRESQL, "VARCHAR(0)"), (POSTGRESQL, "INTEGER(5)"), (SQL_SERVER, "DECIMAL(99,2)"),
                     (POSTGRESQL, "NUMERIC(5,1001)"), (SQL_SERVER, "NUMERIC(5,9)"), (SQL_SERVER, "DECIMAL(3,5)"), (MYSQL, "DECIMAL(3,5)"), (SQL_SERVER, "VARCHAR(9000)"), (SQL_SERVER, "INT(4)"),
                     (MYSQL, "CHAR(300)"), (POSTGRESQL, "VARCHAR(1,2)"), (POSTGRESQL, "TIMESTAMP(9)"),
                     (SQL_SERVER, "DATETIME2(8)"), (MYSQL, "DECIMAL(5,6)")]:
            with self.subTest(d=d, t=t), self.assertRaises(DBAAgentError):
                check_type(t, d, "t.c")

    def test_valid_accepted(self):
        for d, t in [(POSTGRESQL, "VARCHAR(255)"), (POSTGRESQL, "INTEGER"), (POSTGRESQL, "NUMERIC(10,2)"), (POSTGRESQL, "NUMERIC(5,9)"), (POSTGRESQL, "DECIMAL(3,5)"),
                     (POSTGRESQL, "TIMESTAMP(3)"), (POSTGRESQL, "TEXT"), (SQL_SERVER, "DECIMAL(38,2)"),
                     (SQL_SERVER, "NVARCHAR(MAX)"), (SQL_SERVER, "VARCHAR(8000)"), (SQL_SERVER, "DATETIME2(7)"),
                     (SQL_SERVER, "DATETIME"), (MYSQL, "VARCHAR(0)"), (MYSQL, "INT(11) UNSIGNED"),
                     (MYSQL, "DECIMAL(10,2)"), (MYSQL, "ENUM('a','b')"), (MYSQL, "DOUBLE(10,2)"),
                     (SQLITE, "VARCHAR(0)"), (SQLITE, "INTEGER")]:
            with self.subTest(d=d, t=t):
                check_type(t, d, "t.c")


class TemporalTests(unittest.TestCase):
    def c(self, kind, v):
        from dba_agent import seed_data as sd
        return sd.coerce(v, {"kind": kind, "bits": None, "values": None, "national": False}, "postgresql", "x")

    def test_invalid(self):
        for kind, v in [("time", "25:99"), ("time", "12:60"), ("time", "24:00:00"),
                        ("timestamp", "2024-01-01 99:99:99"), ("timestamp", "2024-12-30 27:55:12"),
                        ("timestamp", "2024-01-01 12:00:60"), ("timestamp", "2024-02-30 10:00:00"),
                        ("timestamptz", "2024-01-01 10:00:00+25:00"), ("timestamp", "2024-01-01 1:2:3")]:
            with self.subTest(v=v):
                self.assertIsNotNone(self.c(kind, v)[1])

    def test_valid(self):
        for kind, v, out in [("time", "23:59", "23:59:00"), ("time", "00:00:00", "00:00:00"),
                             ("time", "12:30:45.123456", "12:30:45.123456"),
                             ("timestamp", "2024-01-01 23:59:59", "2024-01-01 23:59:59"),
                             ("timestamp", "2024-12-30T07:55:12.5", "2024-12-30 07:55:12.5"),
                             ("timestamp", "2024-01-01", "2024-01-01 00:00:00"),
                             ("timestamptz", "2024-01-01 10:00:00+05:30", "2024-01-01 10:00:00+05:30"),
                             ("timestamptz", "2024-01-01 10:00:00.250Z", "2024-01-01 10:00:00.250Z")]:
            with self.subTest(v=v):
                self.assertEqual(self.c(kind, v), (out, None))


from dba_agent.errors import LLMError
from dba_agent.state import load_session
from dba_agent.stages import sql_seed
from tests.test_stage6b_seed import SeedStageBase, SeedLLM, clinic_data


class FailedRegenerationTests(SeedStageBase):
    def test_exhausted_retries_do_not_leave_completed_state_on_disk(self):
        self.prepare(sqlite=True)
        res, _ = self.seed(clinic_data())
        self.assertTrue(res.completed)
        old_sha = self.state()["sql_stage"]["seed"]["sha256"]
        bad = clinic_data()
        bad["tables"]["pets"][0]["species"] = "dragon"
        with self.assertRaises(LLMError):
            self.seed(bad, bad, bad, force=True)
        st = load_session(self.tmp.name, self.s.session_id).data      # reloaded from disk
        self.assertNotIn("sql", st["completed_stages"])
        self.assertEqual(st["current_stage"], "sql")
        self.assertEqual(st["sql_stage"]["status"], "in_progress")
        self.assertEqual(st["sql_stage"]["seed"]["status"], "generating")
        self.assertEqual(st["sql_stage"]["seed"]["previous_artifact"]["sha256"], old_sha)
        self.assertTrue(self.path().exists())                          # old file kept as recoverable artifact
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "incomplete")


class ResumeIntegrityTests(SeedStageBase):
    def test_resume_detects_modified_and_missing_seed(self):
        self.prepare(sqlite=True)
        self.seed(clinic_data())
        code, text = self.run_cli("resume")
        self.assertIn("All six stages are complete", text)
        self.path().write_text("-- tampered", encoding="utf-8")
        code, text = self.run_cli("resume")
        self.assertNotIn("All six stages are complete", text)
        self.assertIn("seed.sql: modified", text)
        self.assertIn(f"seed {self.s.session_id} --force", text)
        st = self.state()
        self.assertNotIn("sql", st["completed_stages"])
        self.assertEqual(st["current_stage"], "sql")
        self.seed(clinic_data(), force=True)
        self.path().unlink()
        code, text = self.run_cli("resume")
        self.assertNotIn("All six stages are complete", text)
        self.assertIn("seed.sql: missing_file", text)
        self.assertIn(f"Next: python -m dba_agent seed {self.s.session_id}", text)


# ------------------------------------------------------------------ Task B
import json, tempfile
from pathlib import Path
from dba_agent.config import config_from_dict, load_config
from dba_agent.errors import ConfigError
from dba_agent import seed_data as sd

BASE = {"provider": "ollama", "model": "m"}


def cfg(**kw):
    return config_from_dict(dict(BASE, **kw), env={})


class ConfigTests(unittest.TestCase):
    def test_booleans(self):
        self.assertFalse(cfg(json_mode="false").json_mode)
        self.assertTrue(cfg(json_mode="True").json_mode)
        self.assertFalse(cfg(json_mode=False).json_mode)
        for bad in ("maybe", 2, [], None):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                cfg(json_mode=bad)

    def test_numeric_ranges(self):
        for k, v in [("timeout", -10), ("timeout", 0), ("retry_delay", -3), ("temperature", 99), ("temperature", -1),
                     ("max_attempts", 0), ("max_attempts", 2.5), ("max_attempts", 999), ("timeout", "abc"),
                     ("timeout", True), ("temperature", float("nan")), ("seed_rows", 0), ("seed_rows", -2)]:
            with self.subTest(k=k, v=v), self.assertRaises(ConfigError):
                cfg(**{k: v})
        c = cfg(timeout=30, retry_delay=0, temperature=0, max_attempts=5, seed_rows=3)
        self.assertEqual((c.timeout, c.retry_delay, c.temperature, c.max_attempts), (30.0, 0.0, 0.0, 5))

    def test_malformed_values_give_config_errors(self):
        for kw in ({"base_url": 123}, {"base_url": ["x"]}, {"extra_headers": ["a", "b"]},
                   {"extra_headers": {"A": 1}}, {"extra_headers": "x"}, {"model": 5}, {"api_key": 5},
                   {"validation": []}, {"sessions_dir": 3}):
            with self.subTest(kw=kw), self.assertRaises(ConfigError):
                cfg(**kw)
        self.assertEqual(cfg(extra_headers={"X-A": "b"}).extra_headers, {"X-A": "b"})
        self.assertEqual(cfg(extra_headers=None).extra_headers, {})


class RowTargetTests(SeedStageBase):
    def test_requested_target_is_enforced(self):
        self.prepare(sqlite=True)
        with self.assertRaises(LLMError) as cm:                    # fixture has 3-5 rows; 8 requested
            self.seed(clinic_data(), clinic_data(), clinic_data(), rows=8)
        self.assertIn("at least 8 are required", str(self.s.data.get("last_error") or "") + str(cm.exception)
                      + "")
        res, _ = self.seed(clinic_data(), rows=3)
        self.assertTrue(res.completed)

    def test_invalid_row_counts_rejected_by_stage(self):
        self.prepare(sqlite=True)
        for bad in (0, -1, 1001):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                self.seed(clinic_data(), rows=bad)

    def test_cli_rejects_zero_and_negative_rows(self):
        self.prepare(sqlite=True)
        for n in ("0", "-4"):
            llm = SeedLLM(clinic_data())
            code, text = self.run_cli("seed", ["--rows", n], llm=llm)
            self.assertEqual(code, 2)
            self.assertIn("--rows must be at least 1", text)
            self.assertEqual(llm.calls, [])
        self.assertNotIn("seed", self.state().get("sql_stage", {}))

    def test_infeasible_targets_are_capped_and_reported(self):
        self.prepare(sqlite=True)
        from dba_agent.stages import sql_seed as ss
        design = self.s.data["design"]
        model = sd.build_model(design)
        req, notes = sd.required_rows(model, 8)
        self.assertTrue(all(1 <= n <= 8 for n in req.values()))
        # a unique column limited to 2 allowed values caps its table at 2
        name = next(iter(model.tables))
        t = model.tables[name]
        col = next(c for c in t["columns"] if c not in t["pk"])
        t["uniques"].append({"name": "u_x", "columns": [col]})
        t["checks"].append({"name": "ck_x", "expression": f"{col} IN ('a','b')"})
        req, notes = sd.required_rows(model, 8)
        self.assertEqual(req[name], 2)
        self.assertTrue(any(name in n and "cannot be satisfied" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
