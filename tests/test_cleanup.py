import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codex_cleanup import CleanupError, Settings, apply, load_manifest, scan, verify, write_manifest
from codex_cleanup.__main__ import main
from codex_cleanup.artifacts import (
    append_event,
    apply_lock,
    digest,
    events,
    journal_path,
    journal_state,
)
from codex_cleanup.errors import (
    InterruptedRun,
    ProtocolError,
    StalePlan,
    UnsafeRuntime,
    UnsupportedLayout,
)
from codex_cleanup.runtime import Runtime, RuntimeGuard, require_reviewed
from codex_cleanup.storage import timestamp
from tests.support import OLD, Store, tid


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)


class PolicyTests(Fixture):
    def test_old_archive_only_and_private_bodies_not_saved(self):
        self.store.add(1)
        self.store.add(2, archived=False)
        self.store.add(3, recent=True)
        plan = scan(self.store.settings)
        self.assertEqual(plan.target_ids, {tid(1)})
        self.assertNotIn("PRIVATE_BODY_SENTINEL", json.dumps(plan.to_dict()))
        self.assertNotIn("PRIVATE_TITLE_SENTINEL", json.dumps(plan.to_dict()))

    def test_recent_fork_preserves_old_ancestor(self):
        self.store.add(1)
        self.store.add(2, recent=True, parent=1)
        self.assertEqual(scan(self.store.settings).target_ids, set())

    def test_active_spawn_preserves_entire_parent_boundary(self):
        self.store.add(1)
        self.store.add(2, archived=False)
        self.store.spawn(1, 2)
        self.assertEqual(scan(self.store.settings).target_ids, set())

    def test_spawn_cascade_is_one_group(self):
        self.store.add(1)
        self.store.add(2)
        self.store.spawn(1, 2)
        plan = scan(self.store.settings)
        self.assertEqual(len(plan.groups), 1)
        self.assertEqual(plan.groups[0]["ids"], [tid(1), tid(2)])

    def test_fork_child_precedes_larger_parent(self):
        self.store.add(1, size=1000)
        self.store.add(2, parent=1)
        self.assertEqual([g["root"] for g in scan(self.store.settings).groups], [tid(2), tid(1)])

    def test_largest_ready_leaf_first(self):
        self.store.add(1)
        self.store.add(2, size=1000)
        self.assertEqual(scan(self.store.settings).groups[0]["root"], tid(2))

    def test_pins_background_queue_goals_and_history_protect(self):
        self.store.add(1, pinned=True)
        self.store.add(2, background=1)
        self.store.add(3, background=None)
        self.store.add(4)
        self.store.add(5)
        self.store.add(6)
        self.store.sql("queue_1.sqlite", "INSERT INTO queued_items VALUES (?)", (tid(4),))
        self.store.sql(
            "goals_1.sqlite", "INSERT INTO thread_goals VALUES (?,?)", (tid(5), "running")
        )
        self.store.sql(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
            (tid(6), "inProgress", OLD, None, 1),
        )
        self.assertEqual(scan(self.store.settings).target_ids, set())

    def test_latest_terminal_turn_does_not_hold_forever(self):
        self.store.add(1)
        self.store.sql(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
            (tid(1), "inProgress", OLD, None, 1),
        )
        self.store.sql(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns VALUES (?,?,?,?,?)",
            (tid(1), "completed", OLD, OLD, 2),
        )
        self.assertEqual(scan(self.store.settings).target_ids, {tid(1)})

    def test_exclusions(self):
        self.store.add(1)
        self.assertFalse(scan(replace(self.store.settings, keep_ids=(tid(1),))).target_ids)
        self.assertFalse(scan(replace(self.store.settings, keep_cwds=("/workspace",))).target_ids)

    def test_desktop_pin(self):
        self.store.add(1)
        (self.store.home / ".codex-global-state.json").write_text(
            json.dumps({"pinned-thread-ids": [tid(1)]})
        )
        self.assertFalse(scan(self.store.settings).target_ids)

    def test_spawn_and_fork_cycles_fail_closed(self):
        self.store.add(1, parent=2)
        self.store.add(2, parent=1)
        self.store.add(3)
        self.assertEqual(scan(self.store.settings).target_ids, {tid(3)})
        self.store.spawn(1, 2)
        self.store.spawn(2, 1)
        self.assertEqual(scan(self.store.settings).target_ids, {tid(3)})

    def test_missing_unknown_and_newer_layout_refused(self):
        self.store.add(1)
        self.store.sql(
            "state_5.sqlite", "ALTER TABLE threads ADD COLUMN future_safety_flag INTEGER"
        )
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_new_store_version_refused(self):
        (self.store.home / "state_6.sqlite").touch()
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_compressed_and_symlinked_rollouts_refused(self):
        path = self.store.add(1)
        compressed = path.with_suffix(".jsonl.zst")
        compressed.touch()
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)
        compressed.unlink()
        (path.parent / "alias.jsonl").symlink_to(path)
        with self.assertRaises(UnsupportedLayout):
            scan(self.store.settings)

    def test_timestamp_units_and_invalid_age(self):
        for value in (OLD, OLD * 1000, OLD * 1000000, "2020-01-01T00:00:00Z"):
            self.assertEqual(timestamp(value), OLD)
        for value in (None, "unknown", "2020-01-01", float("nan")):
            self.assertIsNone(timestamp(value))

    def test_settings_defaults_are_rolling_and_invalid_cutoffs_refused(self):
        settings = Settings.default(home=self.store.home, state_dir=self.store.state)
        self.assertLess(
            abs((datetime.now(timezone.utc) - settings.cutoff).total_seconds() - 30 * 86400), 5
        )
        with self.assertRaises(CleanupError):
            replace(settings, cutoff=datetime.now())
        with self.assertRaises(CleanupError):
            replace(settings, cutoff=datetime.now(timezone.utc) + timedelta(days=1))
        with self.assertRaises(CleanupError):
            replace(settings, state_dir=self.store.home / "private")


class ArtifactTests(Fixture):
    def test_round_trip_private_permissions_and_exclusive_write(self):
        self.store.add(1)
        manifest = self.store.manifest()
        self.assertEqual(load_manifest(manifest).target_ids, {tid(1)})
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_manifest(scan(self.store.settings), manifest)
        manifest.chmod(0o644)
        with self.assertRaises(CleanupError):
            load_manifest(manifest)

    def test_checksum_and_rebuilt_policy(self):
        self.store.add(1)
        manifest = self.store.manifest()
        value = json.loads(manifest.read_text())
        value["plan"]["groups"] = []
        manifest.write_text(json.dumps(value))
        with self.assertRaises(StalePlan):
            load_manifest(manifest)
        value["sha256"] = digest(value["plan"])
        manifest.write_text(json.dumps(value))
        with self.assertRaises(CleanupError):
            load_manifest(manifest)

    def test_symlink_manifest_and_lock_conflict(self):
        self.store.add(1)
        manifest = self.store.manifest()
        alias = self.store.state / "alias"
        alias.symlink_to(manifest)
        with self.assertRaises(CleanupError):
            load_manifest(alias)
        with apply_lock(self.store.settings):
            with self.assertRaises(CleanupError):
                with apply_lock(self.store.settings):
                    pass

    def test_unresolved_and_incomplete_journal(self):
        self.store.add(1)
        manifest = self.store.manifest()
        plan = load_manifest(manifest)
        journal = journal_path(manifest)
        append_event(
            journal,
            {
                "kind": "intent",
                "manifest": digest(plan.to_dict()),
                "root": tid(1),
                "ids": [tid(1)],
            },
        )
        with self.assertRaises(InterruptedRun):
            journal_state(plan, events(journal))
        journal.write_text('{"incomplete":')
        with self.assertRaises(InterruptedRun):
            events(journal)


class ApplyTests(Fixture):
    def setUp(self):
        super().setUp()
        # No live process inspection or runtime discovery: only the fixture binary.
        self.guard = patch("codex_cleanup.execution.RuntimeGuard.check", return_value=None)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def run_apply(self, manifest, **kwargs):
        return apply(manifest, confirm=True, binary=self.store.binary, **kwargs)

    def test_confirmation_required_before_any_read(self):
        with self.assertRaises(CleanupError):
            apply("/nonexistent-manifest")

    def test_fake_stdio_success_preserves_unarchived_file(self):
        self.store.add(1)
        survivor = self.store.add(2, archived=False)
        before = survivor.read_bytes()
        manifest = self.store.manifest()
        result = self.run_apply(manifest)
        self.assertEqual(result["deleted_threads"], 1)
        self.assertEqual(result["remaining_requests"], 0)
        self.assertEqual(survivor.read_bytes(), before)
        self.assertTrue(verify(manifest)["verified"])

    def test_spawn_subtree_and_bounded_resume(self):
        self.store.add(1)
        self.store.add(2)
        self.store.spawn(1, 2)
        self.store.add(3)
        manifest = self.store.manifest()
        self.assertEqual(self.run_apply(manifest, max_requests=1)["remaining_requests"], 1)
        with self.assertRaises(StalePlan):
            verify(manifest)
        self.assertEqual(self.run_apply(manifest)["deleted_threads"], 3)

    def test_stale_rollout_stops_before_runtime(self):
        path = self.store.add(1)
        manifest = self.store.manifest()
        with path.open("a") as handle:
            handle.write("{}\n")
        with self.assertRaises(StalePlan):
            self.run_apply(manifest)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_unreviewed_version_stops_without_delete(self):
        self.store.add(1)
        manifest = self.store.manifest()
        with patch.dict(os.environ, {"FAKE_VERSION": "0.999.0"}):
            with self.assertRaises(UnsafeRuntime):
                self.run_apply(manifest)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_wrong_effective_store_stops_without_delete(self):
        self.store.add(1)
        manifest = self.store.manifest()
        other = Store(Path(self.temp.name) / "other")
        with patch.dict(os.environ, {"FAKE_STORE": str(other.home)}):
            with self.assertRaises(UnsafeRuntime):
                self.run_apply(manifest)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_loaded_thread_stops_without_delete(self):
        self.store.add(1)
        manifest = self.store.manifest()
        with patch.dict(os.environ, {"FAKE_LOADED": "1"}):
            with self.assertRaises(ProtocolError):
                self.run_apply(manifest)
        self.assertFalse((self.store.home / ".requests").exists())

    def test_crash_after_delete_is_not_automatically_retried(self):
        self.store.add(1)
        manifest = self.store.manifest()
        with patch.dict(os.environ, {"FAKE_CRASH": "1"}):
            with self.assertRaises(ProtocolError):
                self.run_apply(manifest)
        self.assertTrue(verify(manifest)["verified"])
        with self.assertRaises(InterruptedRun):
            self.run_apply(manifest)
        self.assertEqual(len((self.store.home / ".requests").read_text().splitlines()), 1)

    def test_backend_error_is_private_and_no_retry(self):
        self.store.add(1)
        manifest = self.store.manifest()
        with patch.dict(os.environ, {"FAKE_ERROR": "1"}):
            with self.assertRaises(ProtocolError) as caught:
                self.run_apply(manifest)
        self.assertNotIn("PRIVATE_ERROR_SENTINEL", str(caught.exception))
        with self.assertRaises(InterruptedRun):
            self.run_apply(manifest)

    def test_unexpected_overdelete_stops_without_completion(self):
        self.store.add(1)
        self.store.add(2, archived=False)
        manifest = self.store.manifest()
        with patch.dict(os.environ, {"FAKE_OVERDELETE": "1"}):
            with self.assertRaises(StalePlan):
                self.run_apply(manifest)
        with self.assertRaises(InterruptedRun):
            self.run_apply(manifest)


class CommandTests(Fixture):
    def test_help_version_and_synthetic_scan(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main(["--help"])
            self.assertEqual(result.exception.code, 0)
        self.store.add(1)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "scan",
                    "--home",
                    str(self.store.home),
                    "--state-dir",
                    str(self.store.state),
                    "--sqlite-home",
                    str(self.store.home),
                    "--before",
                    "2021-01-01T00:00:00Z",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["candidate_threads"], 1)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["apply", "/nonexistent-manifest"]), 2)

    def test_runtime_gate_has_no_unknown_version_bypass(self):
        with self.assertRaises(UnsafeRuntime):
            require_reviewed(Runtime(Path("/fixture/runtime"), "0.999.0"))

    def test_runtime_effective_store_requires_exact_match(self):
        with patch("codex_cleanup.runtime.fd_paths", return_value={Path("/other/state_5.sqlite")}):
            with self.assertRaises(UnsafeRuntime):
                RuntimeGuard().verify_store(self.store.settings, 1)
