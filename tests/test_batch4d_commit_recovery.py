"""Batch 4D: atomic Stage 5 commits, version-history integrity and recovery of interrupted commits.

`Crash` derives from BaseException: the commit's rollback handler (`except Exception`) does not run for it, which is what a
killed process looks like on disk. One test additionally kills a real subprocess with os._exit."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import approval, cli, revision, state as state_mod, versions
from dba_agent.state import load_session
from tests.test_stage5a_revision import ScriptedLLM, add_weight, drop_phone, response
from tests.test_stage5b_safety import B

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "sessions" / "20261113-192128-c94d"


class Crash(BaseException):
    """Simulated process death (not an Exception: nothing in the code may catch and 'clean up' after it)."""


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Base4D(B):
    def reload(self):
        self.s = load_session(self.tmp.name, self.s.session_id)
        return self.s

    def disk(self):
        out = self.s.output_dir
        return {n: (out / n).read_text(encoding="utf-8") if (out / n).is_file() else None
                for n in ["design.json", "changelog.md", "dbms-choice.md"] + ["conceptual-model.md", "logical-schema.md",
                                                                               "normalization.md", "assumptions.md"]}

    def state_bytes(self):
        return self.s.state_path.read_bytes()

    def run_recover(self, *argv, inputs=()):
        out, it = [], iter(inputs)

        def read():
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        code = cli.main(["--sessions-dir", self.tmp.name, "recover", self.s.session_id, *argv], input_fn=read, out=out.append)
        return code, "\n".join(map(str, out))

    def fail_write(self, nth, exc):
        """Patch revision._atomic_write so the nth file write of the commit raises exc."""
        real, calls = revision._atomic_write, []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == nth:
                raise exc
            return real(path, text)
        return mock.patch.object(revision, "_atomic_write", flaky)

    def assert_consistent_now(self):
        self.assert_state_files_agree()

    def assert_state_files_agree(self):
        d = self.s.data
        self.assertEqual(json.loads((self.s.output_dir / "design.json").read_text(encoding="utf-8")), d["design"])
        st = revision.commit_status(self.s)
        self.assertEqual(st["state"], "clean", st)
        n = len(d.get("revisions", []))
        self.assertEqual(self.disk()["changelog.md"].count("## Revision "), n)
        if n:
            self.assertEqual(versions.verify_version(self.s, n), [])
            self.assertEqual(versions.load_version(self.s, n)[0]["design_fingerprint"], approval.design_fingerprint(d["design"]))


class FailureBeforeAndDuringWrites(Base4D):
    def test_failure_before_design_is_written_changes_nothing(self):
        self.approve()                                                   # approved design -> an intact approval to protect
        before_files, before_state = self.disk(), copy.deepcopy(self.s.data)
        with self.fail_write(1, OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"], request="weight")
        self.assertEqual(self.disk(), before_files)
        for k in ("design", "revisions", "approval", "current_stage", "completed_stages"):
            self.assertEqual(self.s.data.get(k) or None, before_state.get(k) or None, k)
        self.assertFalse(versions.has_version(self.s, 1))
        self.assertIn("revision_pending", load_session(self.tmp.name, self.s.session_id).data)   # the accepted change is kept

    def test_failed_revision_preserves_an_intact_approval(self):
        ap, _ = self.approve()
        files = {n: sha(self.s.output_dir / n) for n in approval.GUARDED_FILES}
        with self.fail_write(3, OSError("boom")):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"])
        saved = load_session(self.tmp.name, self.s.session_id).data
        self.assertEqual(saved["approval"]["status"], "approved")
        self.assertEqual(saved["approval"]["design_fingerprint"], ap["design_fingerprint"])
        self.assertEqual(saved["current_stage"], "sql")
        self.assertNotIn("approval_history", saved)
        self.assertEqual({n: sha(self.s.output_dir / n) for n in approval.GUARDED_FILES}, files)   # files rolled back
        # abandoning the interrupted revision on purpose leaves the approval fully intact (integrity proven by the hashes)
        code, text = self.run_recover("--discard", "--yes")
        self.assertEqual(code, 0, text)
        self.reload()
        rep = approval.integrity_report(self.s)
        self.assertEqual(rep["blocking"], [])
        self.assertEqual(self.s.data["approval"]["status"], "approved")
        self.assertEqual(self.s.data["revision_discarded"][0]["request"], "change it")

    def test_crash_after_design_written_before_documents_then_recover(self):
        before = self.disk()
        with self.fail_write(3, Crash()):
            with self.assertRaises(Crash):
                self.apply(add_weight, ["add_attribute"], request="weight")
        self.assertNotEqual(self.disk()["design.json"], before["design.json"])      # new design.json, old documents
        self.assertEqual(self.disk()["logical-schema.md"], before["logical-schema.md"])
        s2 = self.reload()
        self.assertEqual(s2.data.get("revisions", []), [])                           # history not committed
        st = revision.commit_status(s2)
        self.assertEqual(st["state"], "pending_recoverable")
        code, text = self.run_recover()
        self.assertEqual(code, 0, text)
        self.reload()
        self.assertEqual([r["number"] for r in self.s.data["revisions"]], [1])
        self.assertNotIn("revision_pending", self.s.data)
        self.assertIn("weight_kg", self.disk()["logical-schema.md"])
        self.assert_state_files_agree()

    def test_failure_while_writing_snapshot_rolls_back_and_leaves_no_partial_folder(self):
        real, n = versions.atomic_write_text, []

        def flaky(path, text):
            if "v001" in str(path):                       # only the snapshot of the revision being committed
                n.append(1)
                if len(n) == 2:
                    raise OSError("snapshot disk error")
            return real(path, text)
        before = self.disk()
        with mock.patch.object(versions, "atomic_write_text", flaky):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"])
        self.assertEqual(self.disk(), before)
        base = self.s.root / "versions"
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["v000"])            # no v001 and no leftover .tmp
        self.assertEqual(self.s.data.get("revisions", []), [])
        rec = revision.finish_pending(self.reload())                                  # retry later succeeds
        self.assertEqual(rec["number"], 1)
        self.assert_state_files_agree()

    def test_crash_while_writing_snapshot_leaves_only_a_temp_folder(self):
        real, n = versions.atomic_write_text, []

        def killed(path, text):
            if "v001" in str(path):
                n.append(1)
                if len(n) == 3:
                    raise Crash()
            return real(path, text)
        with mock.patch.object(versions, "atomic_write_text", killed):
            with self.assertRaises(Crash):
                self.apply(add_weight, ["add_attribute"])
        self.assertFalse(versions.has_version(self.s, 1))                             # an incomplete snapshot is invisible
        self.assertEqual([m["number"] for m in versions.list_versions(self.reload())], [0])
        self.assertEqual(revision.commit_status(self.s)["state"], "pending_recoverable")
        rec = revision.finish_pending(self.s)
        self.assertEqual(rec["number"], 1)
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1])
        self.assert_state_files_agree()

    def _fail_state_commit(self, exc):
        real = state_mod.atomic_write_text

        def flaky(path, text):
            if Path(path).name == "state.json" and '"revision_pending"' not in text:      # the commit-point write
                raise exc
            return real(path, text)
        return mock.patch.object(state_mod, "atomic_write_text", flaky)

    def test_failure_while_saving_state_does_not_half_commit(self):
        self.approve()
        before_state = copy.deepcopy(self.s.data)
        before_files = self.disk()
        with self._fail_state_commit(OSError("cannot save state")):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"])
        for k in ("design", "revisions", "approval", "current_stage", "dbms_choice"):
            self.assertEqual(self.s.data.get(k) or None, before_state.get(k) or None, k)  # memory is not half-applied
        self.assertEqual(self.disk(), before_files)                                       # files rolled back
        self.assertFalse(versions.has_version(self.s, 1))
        saved = load_session(self.tmp.name, self.s.session_id).data
        self.assertEqual(saved["approval"]["status"], "approved")
        self.assertEqual(saved.get("revisions", []), [])
        self.assertIn("revision_pending", saved)

    def test_crash_at_the_state_save_is_recovered_without_losing_the_change(self):
        self.approve()
        with self._fail_state_commit(Crash()):
            with self.assertRaises(Crash):
                self.apply(add_weight, ["add_attribute"], request="weight")
        saved = load_session(self.tmp.name, self.s.session_id)
        self.assertEqual(saved.data["approval"]["status"], "approved")                    # state still old + journal
        self.assertTrue(versions.has_version(saved, 1))                                   # snapshot and files are new
        self.assertEqual(revision.commit_status(saved)["state"], "pending_recoverable")
        review_lines = []
        cli._report_commit_state(saved, review_lines.append)
        self.assertTrue(any("RECOVERY" not in x and "recoverable" in x for x in review_lines))   # resume reports it
        self.s = saved
        rec = revision.finish_pending(saved)
        self.assertEqual(rec["number"], 1)
        self.assertTrue(rec["approval_invalidated"])
        self.assertNotIn("approval", saved.data)                                           # never blessed: approval is gone
        self.assertEqual(saved.data["approval_history"][0]["status"], "invalidated")
        self.assertEqual(versions.list_versions(saved)[-1]["number"], 1)
        self.assert_state_files_agree()


class RecoveryBehaviour(Base4D):
    def crash_mid_commit(self):
        with self.fail_write(4, Crash()):
            with self.assertRaises(Crash):
                self.apply(add_weight, ["add_attribute"], request="weight")
        return self.reload()

    def test_repeated_recovery_is_idempotent(self):
        self.crash_mid_commit()
        self.assertEqual(self.run_recover()[0], 0)
        files, state_text = self.disk(), self.state_bytes()
        code, text = self.run_recover()
        self.assertEqual(code, 0)
        self.assertIn("Nothing to recover", text)
        self.assertEqual(self.run_recover("--discard", "--yes")[0], 0)                    # also a no-op when clean
        self.assertEqual(self.disk(), files)
        self.assertEqual(self.state_bytes(), state_text)
        self.assertIsNone(revision.finish_pending(self.reload()))
        self.assertEqual(len(self.s.data["revisions"]), 1)
        self.assertEqual(self.disk()["changelog.md"].count("## Revision "), 1)

    def test_recovery_after_crash_then_new_revision_gets_next_number(self):
        self.crash_mid_commit()
        self.run_recover()
        self.reload()
        rec = self.rev(lambda cur: response(cur, drop_phone, ["remove_attribute"]), request="second", confirmed=True)
        self.assertEqual(rec["number"], 2)
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1, 2])
        self.assert_state_files_agree()

    def test_hand_edited_design_json_is_recovery_required_and_never_overwritten(self):
        self.crash_mid_commit()
        p = self.s.output_dir / "design.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["project_name"] = "edited by hand"
        p.write_text(json.dumps(data), encoding="utf-8")
        before, state_text = self.disk(), self.state_bytes()
        self.assertEqual(revision.commit_status(self.s)["state"], "recovery_required")
        with self.assertRaises(revision.RecoveryRequired):
            revision.finish_pending(self.s)
        code, text = self.run_recover()
        self.assertEqual(code, 2)
        self.assertIn("RECOVERY REQUIRED", text)
        self.assertEqual(self.disk(), before)                                             # nothing was guessed or changed
        self.assertEqual(self.state_bytes(), state_text)
        code, text = self.run_recover("--discard", "--yes")                               # the explicit way out
        self.assertEqual(code, 0, text)
        self.assertEqual(revision.commit_status(self.reload())["state"], "clean")

    def test_stale_pending_with_wrong_number_is_refused(self):
        self.crash_mid_commit()
        self.s.data["revision_pending"]["record"]["number"] = 7
        self.s.save()
        self.assertEqual(revision.commit_status(self.s)["state"], "recovery_required")
        with self.assertRaises(revision.RecoveryRequired):
            revision.finish_pending(self.s)
        self.assertEqual(self.s.data.get("revisions", []), [])

    def test_pending_without_new_journal_fields_still_recovers(self):                    # state written by an older version
        self.crash_mid_commit()
        for k in ("base_fingerprint", "candidate_fingerprint"):
            self.s.data["revision_pending"].pop(k)
        self.s.save()
        self.assertEqual(revision.finish_pending(self.reload())["number"], 1)

    def test_restore_after_recovery(self):
        original = copy.deepcopy(self.s.data["design"])
        self.crash_mid_commit()
        self.run_recover()
        self.reload()
        rec = revision.restore_version(self.s, 0)
        self.assertEqual(rec["number"], 2)
        self.assertEqual(approval.design_fingerprint(self.s.data["design"]), approval.design_fingerprint(original))
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1, 2])
        self.assert_state_files_agree()
        with self.assertRaises(revision.RevisionRejected):                                # repeating it must not add a duplicate
            revision.restore_version(self.s, 0)
        self.assertEqual(len(self.s.data["revisions"]), 2)

    def test_real_process_kill_mid_commit(self):
        proposal = revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"])),
                                    self.prompts, "weight")
        revision.accept(proposal)
        self.s.data["revision_pending"] = {
            "candidate": proposal.candidate, "record": proposal.record, "dbms_choice": proposal.dbms_choice,
            "accepted": True, "base_fingerprint": proposal.base_fingerprint,
            "candidate_fingerprint": approval.design_fingerprint(proposal.candidate)}
        self.s.save()
        script = (
            "import os, sys\n"
            "from dba_agent import revision, versions\n"
            "from dba_agent.state import load_session\n"
            "s = load_session(sys.argv[1], sys.argv[2])\n"
            "real = versions.write_version\n"
            "def die(*a, **k):\n"
            "    os._exit(9)            # snapshot step: all files are new, state.json is still old\n"
            "versions.write_version = die\n"
            "revision.commit(s, s.data['revision_pending'])\n")
        proc = subprocess.run([sys.executable, "-c", script, self.tmp.name, self.s.session_id], cwd=ROOT,
                              env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        s2 = self.reload()
        self.assertEqual(s2.data.get("revisions", []), [])
        self.assertIn("weight_kg", self.disk()["logical-schema.md"])                      # files already new
        self.assertEqual(revision.commit_status(s2)["state"], "pending_recoverable")
        self.assertEqual(self.run_recover()[0], 0)
        self.reload()
        self.assertEqual(len(self.s.data["revisions"]), 1)
        self.assert_state_files_agree()


class VersionHistoryIntegrity(Base4D):
    def test_numbers_are_sequential_and_failures_do_not_consume_them(self):
        with self.fail_write(1, OSError("x")):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"], request="first try")
        self.assertEqual(self.run_recover("--discard", "--yes")[0], 0)
        self.reload()
        self.assertEqual(self.apply(add_weight, ["add_attribute"], request="again")["number"], 1)
        self.assertEqual(self.apply(drop_phone, ["remove_attribute"], confirmed=True)["number"], 2)
        self.assertEqual([r["number"] for r in self.s.data["revisions"]], [1, 2])
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1, 2])
        self.assertEqual(self.disk()["changelog.md"].count("## Revision "), 2)

    def test_snapshots_match_their_fingerprints(self):
        self.apply(add_weight, ["add_attribute"])
        self.apply(drop_phone, ["remove_attribute"], confirmed=True)
        for m in versions.list_versions(self.s):
            meta, design, _ = versions.load_version(self.s, m["number"])
            self.assertEqual(meta["design_fingerprint"], approval.design_fingerprint(design))
        self.assertEqual(versions.load_version(self.s, 2)[0]["design_fingerprint"],
                         approval.design_fingerprint(self.s.data["design"]))

    def test_valid_snapshot_is_never_overwritten(self):
        self.apply(add_weight, ["add_attribute"])
        folder = versions.version_dir(self.s, 1)
        before = {p.name: sha(p) for p in folder.iterdir()}
        with self.assertRaises(versions.VersionConflict):
            versions.write_version(self.s, 1, {"design.json": "{}"}, {"design_fingerprint": "different"}, committed=1)
        self.assertEqual({p.name: sha(p) for p in folder.iterdir()}, before)
        same = versions.load_version(self.s, 1)[0]                                        # same fingerprint: kept as it is
        self.assertFalse(versions.write_version(self.s, 1, {"design.json": "{}"}, same, committed=1))
        self.assertEqual({p.name: sha(p) for p in folder.iterdir()}, before)

    def test_orphan_snapshot_of_an_uncommitted_number_is_quarantined_not_deleted(self):
        versions.write_version(self.s, 1, {"design.json": "{}"}, {"design_fingerprint": "orphan"}, committed=0)
        self.assertTrue(versions.has_version(self.s, 1))
        rec = self.apply(add_weight, ["add_attribute"])                                   # the real revision 1 replaces it
        self.assertEqual(rec["number"], 1)
        self.assertEqual(versions.verify_version(self.s, 1), [])
        kept = list((self.s.root / "versions" / versions.QUARANTINE).iterdir())
        self.assertEqual(len(kept), 1)
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1])   # not listed twice

    def test_tampered_snapshot_is_detected_before_restore(self):
        self.apply(add_weight, ["add_attribute"])
        p = versions.version_dir(self.s, 0) / "design.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["project_name"] = "tampered"
        p.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(Exception) as cm:
            revision.restore_version(self.s, 0)
        self.assertIn("fingerprint", str(cm.exception))
        self.assertEqual(len(self.s.data["revisions"]), 1)

    def test_rejected_revision_leaves_everything_unchanged(self):
        self.apply(add_weight, ["add_attribute"])
        before, st = self.disk(), self.state_bytes()
        llm = ScriptedLLM(self.s, lambda cur: response(cur, drop_phone, ["remove_attribute"]))
        self.assertIsNone(revision.revise(self.s, llm, self.prompts, "drop phone", confirm=lambda p, r: False))
        self.assertEqual(self.disk(), before)
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1])
        self.assertEqual(len(self.s.data["revisions"]), 1)
        self.assertNotIn("revision_pending", self.s.data)

    def test_double_commit_of_one_proposal_creates_one_revision(self):
        llm = ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"]))
        proposal = revision.propose(self.s, llm, self.prompts, "weight")
        revision.accept(proposal)
        revision.commit_accepted(self.s, proposal)
        with self.assertRaises(revision.RevisionRejected):
            revision.commit_accepted(self.s, proposal)
        self.assertEqual(len(self.s.data["revisions"]), 1)


class SuccessfulCommitAndApproval(Base4D):
    def test_successful_revision_invalidates_approval_and_state_is_consistent(self):
        ap, _ = self.approve()
        rec = self.apply(add_weight, ["add_attribute"])
        self.assertTrue(rec["approval_invalidated"])
        saved = load_session(self.tmp.name, self.s.session_id).data
        self.assertNotIn("approval", saved)
        self.assertEqual(saved["approval_history"][0]["design_fingerprint"], ap["design_fingerprint"])
        self.assertEqual(saved["approval_history"][0]["status"], "invalidated")
        self.assertEqual(saved["current_stage"], "revision")
        self.assertNotIn("revision_pending", saved)
        self.assert_state_files_agree()

    def test_no_temp_files_are_left_after_a_normal_commit(self):
        self.apply(add_weight, ["add_attribute"])
        left = [p for p in self.s.root.rglob("*") if p.name.endswith(".tmp")]
        self.assertEqual(left, [])


@unittest.skipUnless(ARCHIVE.is_dir(), "archived session fixture not present")
class HistoricalSessionsStayReadable(unittest.TestCase):
    def test_recover_and_status_do_not_touch_the_archived_session(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(ARCHIVE, Path(tmp) / ARCHIVE.name)
        before = {str(p.relative_to(tmp)): sha(p) for p in Path(tmp).rglob("*") if p.is_file()}
        s = load_session(tmp, ARCHIVE.name)
        self.assertEqual(revision.commit_status(s)["state"], "clean")
        lines = []
        self.assertEqual(cli.main(["--sessions-dir", tmp, "recover", ARCHIVE.name], out=lines.append), 0)
        self.assertIn("Nothing to recover", "\n".join(lines))
        for n in (0, 1):
            self.assertEqual(versions.verify_version(s, n), [])                           # old snapshots verify unchanged
        self.assertEqual({str(p.relative_to(tmp)): sha(p) for p in Path(tmp).rglob("*") if p.is_file()}, before)
        # and the real archive was never modified by this test run
        self.assertTrue(ARCHIVE.is_dir())


if __name__ == "__main__":
    unittest.main()
