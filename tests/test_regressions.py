import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from codex_cleanup import CleanupError, apply, load_manifest, scan, verify, write_manifest
from codex_cleanup.artifacts import apply_lock, digest, journal_state
from codex_cleanup.errors import InterruptedRun, ProtocolError, StalePlan, UnsupportedLayout
from codex_cleanup.protocol import StdioClient
from codex_cleanup.runtime import Runtime, foreign_writable_files
from codex_cleanup.storage import source_references
from tests.support import OLD, Store, tid
from tests.test_cleanup import Fixture


class RegressionTests(Fixture):
    def test_malformed_falsy_fork_ancestry_refused(self):
        self.store.add(1)
        child = self.store.add(2, archived=False, parent=1)
        lines = child.read_text().splitlines(keepends=True)
        for value in ("", [], {}, 0, False):
            with self.subTest(value=value):
                envelope = json.loads(lines[0])
                envelope["payload"]["forked_from_id"] = value
                child.write_text(json.dumps(envelope) + "\n" + "".join(lines[1:]))
                with self.assertRaises(CleanupError):
                    scan(self.store.settings)

    def test_recent_archive_or_recency_date_protects_old_body(self):
        self.store.add(1)
        self.store.sql("state_5.sqlite", "ALTER TABLE threads ADD COLUMN recency_at_ms INTEGER")
        self.store.sql("state_5.sqlite", "UPDATE threads SET recency_at_ms=?", (2000000000000,))
        self.assertFalse(scan(self.store.settings).target_ids)

    def test_missing_survivor_rollout_blocks_uncertain_ancestry(self):
        self.store.add(1)
        survivor = self.store.add(2, archived=False, parent=1)
        survivor.unlink()
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_non_session_first_record_refused(self):
        path = self.store.add(1)
        path.write_text(json.dumps({"type": "event_msg", "payload": {"id": tid(1)}}) + "\n")
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_outside_and_mismatched_backend_path_refused(self):
        self.store.add(1)
        self.store.sql(
            "state_5.sqlite",
            "UPDATE threads SET rollout_path=? WHERE id=?",
            (str(self.store.root / "outside.jsonl"), tid(1)),
        )
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)
        self.store.sql(
            "state_5.sqlite",
            "UPDATE threads SET rollout_path=? WHERE id=?",
            (str(self.store.home / "archived_sessions/missing.jsonl"), tid(1)),
        )
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_workspace_symlink_keep_protects_ancestor(self):
        self.store.add(1)
        self.store.add(2, parent=1)
        kept = self.store.root / "workspace"
        kept.mkdir()
        alias = self.store.root / "alias"
        alias.symlink_to(kept, target_is_directory=True)
        self.store.sql(
            "state_5.sqlite", "UPDATE threads SET cwd=? WHERE id=?", (str(alias), tid(2))
        )
        settings = replace(self.store.settings, keep_cwds=(str(kept),))
        self.assertFalse(scan(settings).target_ids)

    def test_uncertain_source_never_discards_ancestry(self):
        for value in (
            '{"subagent":',
            '{"future_parent": "hidden"}',
            '"future-source"',
            '{"parent_thread_id": null}',
            '{"parent_thread_id": "invalid"}',
        ):
            with self.subTest(value=value):
                with self.assertRaises(CleanupError):
                    source_references(value)
        self.assertEqual(
            source_references(
                json.dumps(
                    {
                        "subagent": {"thread_spawn": {"parent_thread_id": tid(1), "depth": 1}},
                    }
                )
            ),
            [tid(1)],
        )

    def test_workspace_alias_retarget_is_stale(self):
        self.store.add(1)
        first = self.store.root / "first"
        kept = self.store.root / "kept"
        first.mkdir()
        kept.mkdir()
        alias = self.store.root / "alias"
        alias.symlink_to(first, target_is_directory=True)
        self.store.sql("state_5.sqlite", "UPDATE threads SET cwd=?", (str(alias),))
        plan = scan(replace(self.store.settings, keep_cwds=(str(kept),)))
        self.assertEqual(plan.target_ids, {tid(1)})
        alias.unlink()
        alias.symlink_to(kept, target_is_directory=True)
        with self.assertRaises(StalePlan):
            verify(plan, set())

    def test_shared_writable_store_refused_before_runtime(self):
        self.store.add(1)
        manifest = self.store.manifest()
        self.store.home.chmod(0o770)
        try:
            with self.assertRaises(CleanupError):
                apply(manifest, confirm=True, binary=self.store.binary)
        finally:
            self.store.home.chmod(0o755)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_survivor_history_goal_index_and_desktop_changes_detected(self):
        self.store.add(1)
        self.store.add(2, archived=False)
        self.store.sql(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
            (tid(2), "completed", OLD, OLD, 1),
        )
        self.store.sql(
            "goals_1.sqlite", "INSERT INTO thread_goals VALUES (?,?)", (tid(2), "completed")
        )
        plan = scan(self.store.settings)
        self.store.sql(
            "thread_history_1.sqlite", "DELETE FROM thread_turns WHERE thread_id=?", (tid(2),)
        )
        with self.assertRaises(StalePlan):
            verify(plan, set())
        self.store.sql(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
            (tid(2), "completed", OLD, OLD, 1),
        )
        self.store.sql("goals_1.sqlite", "DELETE FROM thread_goals WHERE thread_id=?", (tid(2),))
        with self.assertRaises(StalePlan):
            verify(plan, set())
        self.store.sql(
            "goals_1.sqlite", "INSERT INTO thread_goals VALUES (?,?)", (tid(2), "completed")
        )
        index = self.store.home / "session_index.jsonl"
        before = index.read_text()
        index.write_text(
            "".join(
                line
                for line in before.splitlines(keepends=True)
                if json.loads(line)["id"] != tid(2)
            )
        )
        with self.assertRaises(StalePlan):
            verify(plan, set())
        index.write_text(before)
        (self.store.home / ".codex-global-state.json").write_text('{"unrelated": "changed"}')
        with self.assertRaises(StalePlan):
            verify(plan, set())

    def test_malformed_index_blocks_before_request(self):
        self.store.add(1)
        manifest = self.store.manifest()
        (self.store.home / "session_index.jsonl").write_text("incomplete")
        with self.assertRaises(UnsupportedLayout):
            apply(manifest, confirm=True, binary=self.store.binary)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_lock_is_shared_across_state_and_codex_homes(self):
        other = Store(self.store.root / "other")
        for settings in (replace(self.store.settings, state_dir=other.state), other.settings):
            with apply_lock(self.store.settings):
                with self.assertRaises(CleanupError):
                    with apply_lock(settings):
                        pass

    def test_renamed_writable_process_is_detected_without_running_it(self):
        output = f"p99999\nf4\nau\nn{self.store.home}/state_5.sqlite\n"
        result = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with (
            patch("codex_cleanup.runtime.sys.platform", "darwin"),
            patch("codex_cleanup.runtime.shutil.which", return_value="/fixture/lsof"),
            patch("codex_cleanup.runtime.subprocess.run", return_value=result),
        ):
            self.assertTrue(foreign_writable_files(self.store.settings, None))
            self.assertFalse(foreign_writable_files(self.store.settings, 99999))

    def test_spawn_cycle_propagates_to_fork_ancestors(self):
        self.store.add(1)
        self.store.add(2, parent=1)
        self.store.add(3)
        self.store.add(4)
        self.store.spawn(2, 3)
        self.store.spawn(3, 2)
        self.assertEqual(scan(self.store.settings).target_ids, {tid(4)})

    def test_journal_requires_start_root_and_approved_order(self):
        self.store.add(1)
        self.store.add(2, parent=1)
        plan = load_manifest(self.store.manifest())
        identity = digest(plan.to_dict())
        start = {"kind": "start", "manifest": identity}
        intent = {"kind": "intent", "manifest": identity, "root": tid(1), "ids": [tid(1)]}
        for records in (
            [intent],
            [start, intent],
            [start, {"kind": "complete", "manifest": identity}],
        ):
            with self.assertRaises(InterruptedRun):
                journal_state(plan, records)

    def test_duplicate_latest_ordinal_refused(self):
        self.store.add(1)
        for status in ("completed", "inProgress"):
            self.store.sql(
                "thread_history_1.sqlite",
                "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
                (tid(1), status, OLD, OLD, 1),
            )
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_duplicate_rollouts_are_held(self):
        original = self.store.add(1)
        duplicate = original.parent / "duplicate.jsonl"
        duplicate.write_bytes(original.read_bytes())
        os.utime(duplicate, (OLD, OLD))
        self.assertFalse(scan(self.store.settings).target_ids)

    def test_manifest_output_inside_source_home_refused(self):
        self.store.add(1)
        plan = scan(self.store.settings)
        with self.assertRaises(CleanupError):
            write_manifest(plan, self.store.home / "scan.json")
        self.assertFalse((self.store.home / "scan.json").exists())

    def test_selector_failure_reaps_owned_fake_process(self):
        children = []
        original = subprocess.Popen

        def remember(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process

        with (
            patch("codex_cleanup.protocol.subprocess.Popen", side_effect=remember),
            patch("codex_cleanup.protocol.selectors.DefaultSelector", side_effect=OSError),
        ):
            with self.assertRaises(OSError):
                StdioClient(Runtime(self.store.binary, "0.162.1"), self.store.settings)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_method_bearing_and_noninteger_id_frames_refused(self):
        self.store.add(1)
        manifest = self.store.manifest()
        for mode in ("request", "boolean-id", "float-id"):
            with (
                patch("codex_cleanup.execution.RuntimeGuard.check", return_value=None),
                patch.dict(os.environ, {"FAKE_FRAME": mode}),
            ):
                with self.assertRaises(ProtocolError):
                    apply(manifest, confirm=True, binary=self.store.binary)
        self.assertFalse((self.store.home / ".requests").exists())
