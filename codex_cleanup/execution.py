"""Deletion through the reviewed Codex API, never SQL or filesystem removal."""

from pathlib import Path

from .artifacts import (
    append_event,
    apply_lock,
    digest,
    events,
    journal_path,
    journal_state,
    load_manifest,
)
from .errors import CleanupError, StalePlan
from .protocol import StdioClient
from .runtime import RuntimeGuard, discover, require_private_store, require_reviewed
from .storage import json_file, readonly, snapshot, without_deleted


def check_snapshot(plan, deleted):
    expected = without_deleted(plan.snapshot, deleted)
    actual = without_deleted(snapshot(plan.settings), set())
    if actual != expected:
        raise StalePlan(
            "Store, activity, dependencies, or rollout files changed; review a new scan."
        )


def verify(plan, deleted=None):
    """Read-only exact preservation/removal verification; raises on any mismatch."""
    if not hasattr(plan, "snapshot"):
        plan = load_manifest(plan)
    deleted = plan.target_ids if deleted is None else set(deleted)
    if not deleted <= plan.target_ids:
        raise CleanupError("Verification IDs must be part of the reviewed plan.")
    check_snapshot(plan, deleted)
    for tid in deleted:
        if any(Path(f["path"]).exists() for f in plan.snapshot["files"].get(tid, [])):
            raise StalePlan("A targeted rollout still exists.")
    stores = (
        ("state_5.sqlite", "threads", "id"),
        ("thread_history_1.sqlite", "thread_turns", "thread_id"),
        ("goals_1.sqlite", "thread_goals", "thread_id"),
        ("queue_1.sqlite", "queued_items", "thread_id"),
    )
    for name, table, column in stores:
        with readonly(plan.settings.sqlite_home / name) as db:
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise StalePlan("SQLite integrity verification failed.")
            if deleted & {row[0] for row in db.execute(f'SELECT "{column}" FROM "{table}"')}:
                raise StalePlan("A targeted ID remains in a supporting store.")
    index = plan.settings.home / "session_index.jsonl"
    if index.exists():
        if index.is_symlink():
            raise StalePlan("Session index is symlinked.")
        import json

        with index.open("rb") as handle:
            while True:
                line = handle.readline(256 * 1024 + 1)
                if not line:
                    break
                if len(line) > 256 * 1024 or not line.endswith(b"\n"):
                    raise StalePlan("Session index contains an incomplete/oversized record.")
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                        raise ValueError()
                except ValueError:
                    raise StalePlan("Unknown session index format.") from None
                if value["id"] in deleted:
                    raise StalePlan("A targeted ID remains in the session index.")
    # Parse the state again rather than deleting stale UI references ourselves.
    json_file(plan.settings.home / ".codex-global-state.json", optional=True)
    return {"verified": True, "deleted_threads": len(deleted)}


def apply(manifest, *, confirm=False, max_requests=100, binary=None):
    """Apply a bounded number of reviewed requests. Never implicitly retry."""
    if confirm is not True:
        raise CleanupError("Deletion requires explicit confirm=True / --confirm-delete.")
    if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests < 1:
        raise CleanupError("max_requests must be a positive integer.")
    plan = load_manifest(manifest)
    journal = journal_path(manifest)
    with apply_lock(plan.settings):
        records = events(journal)
        done, deleted = journal_state(plan, records)
        check_snapshot(plan, deleted)
        remaining = [group for group in plan.groups if group["root"] not in done]
        if not remaining:
            return dict(verify(plan, deleted), remaining_requests=0)
        require_private_store(plan.settings)
        runtime = discover(plan.settings, binary)
        require_reviewed(runtime)
        guard = RuntimeGuard()
        guard.check(plan.settings)
        verify(plan, deleted)
        identity = digest(plan.to_dict())
        if not records:
            append_event(journal, {"kind": "start", "manifest": identity})
        client = StdioClient(runtime, plan.settings)
        try:
            client.initialize()
            guard.verify_store(plan.settings, client.pid)
            check_snapshot(plan, deleted)
            completed = 0
            for group in remaining[:max_requests]:
                guard.check(plan.settings, client.pid)
                guard.verify_store(plan.settings, client.pid)
                check_snapshot(plan, deleted)
                append_event(
                    journal,
                    {
                        "kind": "intent",
                        "manifest": identity,
                        "root": group["root"],
                        "ids": group["ids"],
                    },
                )
                response = client.delete(group["root"])
                if response != {}:
                    raise CleanupError(
                        "Unexpected deletion acknowledgement; manual review required."
                    )
                new_deleted = deleted | set(group["ids"])
                verify(plan, new_deleted)
                append_event(
                    journal,
                    {
                        "kind": "complete",
                        "manifest": identity,
                        "root": group["root"],
                    },
                )
                deleted = new_deleted
                completed += 1
        finally:
            client.close()
        result = verify(plan, deleted)
        return dict(result, remaining_requests=len(remaining) - completed)
