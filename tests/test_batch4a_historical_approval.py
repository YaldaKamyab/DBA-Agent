"""Batch 4A: historical approval compatibility.

An approved session whose files are byte-identical to what was approved must not be called stale/tampered just because the
upgraded normalization analyzer / validators disagree with the (unchanged) documents. Integrity (recorded hashes) and
compliance with the current rules are separate facts. The archived session in sessions/ is only used as a read-only fixture
(it is copied to a temp dir; nothing is special-cased by its id)."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dba_agent import approval, cli
from dba_agent.schema_sql import SchemaGenerationError, generate_schema_sql
from dba_agent.stages import sql_schema, sql_seed
from dba_agent.state import load_session

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "sessions" / "20261113-192128-c94d"


class NoLLM:
    def __getattr__(self, name):
        raise AssertionError("an LLM call was attempted; legacy recovery must be offline")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(ARCHIVE.is_dir(), "archived session fixture not present")
class HistoricalApprovalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(ARCHIVE, Path(self.tmp) / ARCHIVE.name)
        self.s = load_session(self.tmp, ARCHIVE.name)
        self.out = self.s.output_dir

    def reload(self):
        self.s = load_session(self.tmp, ARCHIVE.name)
        return self.s

    def snapshot(self, names):
        return {n: sha(self.out / n) for n in names}

    def cli(self, *argv):
        lines = []
        rc = cli.main(["--sessions-dir", self.tmp, argv[0], ARCHIVE.name, *argv[1:]], out=lines.append)
        return rc, "\n".join(lines)


class IntegrityVsCompliance(HistoricalApprovalBase):
    def test_unchanged_historical_approval_is_intact_but_needs_revalidation(self):
        rep = approval.integrity_report(self.s)
        self.assertEqual(rep["state"], approval.LEGACY)
        self.assertEqual(rep["blocking"], [])                       # no tampering
        self.assertTrue(any("normalization.md" in f for f in rep["revalidation"]))   # the newer analyzer disagrees
        self.assertEqual(approval.integrity_problems(self.s, allow_legacy=True), [])
        self.assertTrue(approval.integrity_problems(self.s))        # strict callers are still blocked: not silently approved
        ap, design = approval.verify_approved_design(self.s, allow_legacy=True)
        self.assertEqual(approval.design_fingerprint(design), ap["design_fingerprint"])
        with self.assertRaises(approval.ApprovalError):
            approval.verify_approved_design(self.s)

    def test_verification_never_rewrites_historical_files(self):
        names = ["design.json", "normalization.md", "assumptions.md", "schema.sql", "seed.sql", "seed-data.json"]
        before, state = self.snapshot(names), sha(self.s.state_path)
        approval.integrity_report(self.s)
        sql_schema.schema_status(self.s)
        sql_seed.seed_status(self.s)
        self.assertEqual(self.snapshot(names), before)
        self.assertEqual(sha(self.s.state_path), state)

    def test_actual_tampering_is_blocking_and_stale(self):
        (self.out / "normalization.md").write_text((self.out / "normalization.md").read_text("utf-8") + "\nextra\n", "utf-8")
        rep = approval.integrity_report(self.s)
        self.assertEqual(rep["state"], approval.TAMPERED)
        self.assertEqual(rep["revalidation"], [])
        self.assertTrue(any("normalization.md was modified after approval" in p for p in rep["blocking"]))
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "stale")
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "stale")
        with self.assertRaises(approval.ApprovalError):
            approval.verify_approved_design(self.s, allow_legacy=True)

    def test_tampered_design_json_and_session_design_are_blocking(self):
        design = json.loads((self.out / "design.json").read_text("utf-8"))
        design["project_name"] += " (edited)"
        (self.out / "design.json").write_text(json.dumps(design, indent=2), "utf-8")
        self.assertEqual(approval.integrity_report(self.s)["state"], approval.TAMPERED)
        self.setUp()                                                  # fresh copy: edit the design inside state.json instead
        self.s.data["design"]["project_name"] += " (edited)"
        self.assertEqual(approval.integrity_report(self.s)["state"], approval.TAMPERED)

    def test_tampered_sql_files_are_modified_not_legacy(self):
        (self.out / "schema.sql").write_text((self.out / "schema.sql").read_text("utf-8") + "\n-- x\n", "utf-8")
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "modified")
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "stale")     # builds on an untrusted schema.sql

    def test_approval_without_recorded_hashes_is_not_trusted(self):
        self.s.data["approval"]["artifact_hashes"] = None
        rep = approval.integrity_report(self.s)
        self.assertEqual(rep["state"], approval.UNVERIFIED)
        self.assertEqual(rep["revalidation"], [])
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "stale")

    def test_missing_document_is_tampering(self):
        (self.out / "normalization.md").unlink()
        self.assertEqual(approval.integrity_report(self.s)["state"], approval.TAMPERED)


class StatusBehaviour(HistoricalApprovalBase):
    def test_status_is_not_stale(self):
        sch, seed = sql_schema.schema_status(self.s), sql_seed.seed_status(self.s)
        self.assertEqual(sch["state"], "legacy")
        self.assertEqual(seed["state"], "coverage_incomplete")       # the real, recoverable condition is reported
        for st in (sch, seed):
            self.assertNotIn("not (or no longer) approved", st["detail"])
            self.assertIn("revise", st["detail"])                      # and says how to revalidate

    def test_cli_status_commands(self):
        rc, text = self.cli("schema", "--status")
        self.assertEqual(rc, 1)                                       # not "current": it is not validated against current rules
        self.assertIn("schema.sql: legacy", text)
        self.assertNotIn("stale", text)
        rc, text = self.cli("seed", "--status")
        self.assertNotIn("stale", text)
        self.assertIn("seed.sql: coverage_incomplete", text)
        self.assertIn("Stage 6: not complete", text)
        self.assertEqual(rc, 1)


class LegacySeedRecovery(HistoricalApprovalBase):
    def test_offline_coverage_repair_works_without_llm_and_keeps_everything_else(self):
        frozen = ["design.json", "conceptual-model.md", "logical-schema.md", "normalization.md", "assumptions.md",
                  "dbms-choice.md", "schema.sql"]
        before, rec_before = self.snapshot(frozen), copy.deepcopy(self.s.data["approval"])
        res = sql_seed.generate(self.s, NoLLM(), None)
        self.assertTrue(res.changed)
        self.assertTrue(res.reused_data)
        self.assertEqual([r["column"] for r in res.record["offline_repairs"]], ["changed_fields"])
        self.assertEqual(res.report["coverage_missing"] if res.report.get("coverage_missing") else [], [])
        self.assertTrue(res.report["ok"])
        self.assertEqual(self.snapshot(frozen), before)               # approved artifacts untouched
        self.assertEqual(self.s.data["approval"], rec_before)         # approval record not rewritten
        self.reload()
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "legacy")
        # validated, but a defective legacy design is never silently completed
        self.assertNotIn("sql", self.s.data["completed_stages"])
        self.assertEqual(self.s.data["sql_stage"]["status"], "in_progress")
        self.assertIn("revalidation", self.s.data["sql_stage"]["note"])
        res2 = sql_seed.generate(self.s, NoLLM(), None)               # idempotent
        self.assertFalse(res2.changed)

    def test_no_saved_data_means_no_llm_and_nothing_written(self):
        (self.out / "seed-data.json").unlink()
        names = ["seed.sql", "schema.sql"]
        before, state = self.snapshot(names), sha(self.s.state_path)
        with self.assertRaises(approval.ApprovalError) as cm:
            sql_seed.generate(self.s, NoLLM(), None)
        self.assertIn("revise", str(cm.exception))
        self.assertEqual(self.snapshot(names), before)
        self.assertEqual(sha(self.s.state_path), state)
        self.assertFalse((self.out / "seed-data.json").exists())

    def test_force_and_regenerate_are_blocked_for_legacy_designs(self):
        before = self.snapshot(["seed.sql", "seed-data.json", "schema.sql"])
        for kw in ({"force": True}, {"regenerate": True}):
            with self.assertRaises(approval.ApprovalError):
                sql_seed.generate(self.s, NoLLM(), None, **kw)
        self.assertEqual(self.snapshot(["seed.sql", "seed-data.json", "schema.sql"]), before)

    def test_tampered_session_cannot_be_recovered(self):
        (self.out / "assumptions.md").write_text("changed", "utf-8")
        before = self.snapshot(["seed.sql", "seed-data.json"])
        with self.assertRaises(approval.ApprovalError):
            sql_seed.generate(self.s, NoLLM(), None)
        self.assertEqual(self.snapshot(["seed.sql", "seed-data.json"]), before)


class UnsafeSqlRegenerationStaysBlocked(HistoricalApprovalBase):
    def test_schema_cannot_be_regenerated_from_the_malformed_partial_index(self):
        before = self.snapshot(["schema.sql"])
        for force in (False, True):
            with self.assertRaises(approval.ApprovalError):
                sql_schema.generate(self.s, force=force)
        self.assertEqual(self.snapshot(["schema.sql"]), before)
        self.assertFalse((self.out / "schema.sql.tmp").exists())

    def test_batch2_safeguard_in_generator_itself_is_intact(self):
        with self.assertRaises(SchemaGenerationError) as cm:
            generate_schema_sql(self.s.data["design"], "f" * 64)
        self.assertIn("uq_borrowing_records_active_copy", str(cm.exception))
        self.assertIn("conditional", str(cm.exception))

    def test_cli_schema_refuses_and_writes_nothing(self):
        before = self.snapshot(["schema.sql"])
        rc, text = self.cli("schema", "--force")
        self.assertEqual(rc, 2)
        self.assertIn("CANNOT GENERATE SQL", text)
        self.assertEqual(self.snapshot(["schema.sql"]), before)


class NoSessionSpecialCasing(unittest.TestCase):
    def test_no_session_id_in_source(self):
        for rel in ("approval.py", "stages/sql_schema.py", "stages/sql_seed.py"):
            self.assertNotIn("c94d", (ROOT / "dba_agent" / rel).read_text("utf-8"))
            self.assertNotIn("20261113", (ROOT / "dba_agent" / rel).read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
