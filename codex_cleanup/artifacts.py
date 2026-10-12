"""Private local artifacts. A checksum is corruption detection, not a signature."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from .errors import CleanupError, InterruptedRun, StalePlan
from .models import CleanupPlan, Settings, thread_id
from .policy import build_plan

FORMAT = 1
MAX_MANIFEST = 64 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise CleanupError("Artifact directory must not be symlinked.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise CleanupError("Artifact directory must be owned and not group/world writable.")
    sync_directory(path.parent)
    return path


def private_fd(path, flags):
    path = Path(path)
    if path.is_symlink():
        raise CleanupError("Artifact file must not be symlinked.")
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise CleanupError("Artifacts must be regular, owned files with mode 0600.")
    return fd


def write_manifest(plan, path):
    path = Path(path).expanduser().resolve()
    if any(
        path == root or root in path.parents
        for root in (plan.settings.home, plan.settings.sqlite_home)
    ):
        raise CleanupError("Manifests must be outside the read-only Codex and SQLite homes.")
    private_directory(path.parent)
    payload = plan.to_dict()
    envelope = {"format": FORMAT, "sha256": digest(payload), "plan": payload}
    fd = private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical(envelope) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)
    return path


def load_manifest(path):
    fd = private_fd(Path(path).expanduser(), os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise CleanupError("Manifest exceeds the supported size.")
    try:
        envelope = json.loads(raw)
        if set(envelope) != {"format", "sha256", "plan"} or envelope["format"] != FORMAT:
            raise CleanupError("Unsupported manifest format.")
        payload = envelope["plan"]
        if digest(payload) != envelope["sha256"]:
            raise StalePlan("Manifest checksum does not match.")
        if set(payload) != {"settings", "snapshot", "groups", "holds", "created_at"}:
            raise CleanupError("Unknown manifest fields.")
        settings = Settings.from_dict(payload["settings"])
        snapshot = payload["snapshot"]
        if set(snapshot) != {
            "threads",
            "files",
            "references",
            "spawn",
            "activity",
            "last_activity",
            "unknown_age",
            "support",
            "index",
            "desktop_hash",
        }:
            raise CleanupError("Unknown snapshot fields.")
        for tid in set(snapshot["threads"]) | set(snapshot["files"]):
            thread_id(tid)
        for tid, row in snapshot["threads"].items():
            path = Path(row["path"])
            roots = (settings.home / "sessions", settings.home / "archived_sessions")
            if (
                not path.is_absolute()
                or path != path.resolve()
                or not any(root in path.parents for root in roots)
            ):
                raise CleanupError("Backend rollout path is not canonical and confined.")
            matches = [r for r in snapshot["files"].get(tid, []) if r["path"] == row["path"]]
            if row["archived"] and tid in {t for group in payload["groups"] for t in group["ids"]}:
                if len(matches) != 1 or not matches[0]["archived"]:
                    raise CleanupError(
                        "Target metadata does not own the reviewed archived rollout."
                    )
        for files in snapshot["files"].values():
            for record in files:
                file = Path(record["path"])
                if not file.is_absolute() or file != file.resolve():
                    raise CleanupError("Manifest rollout path must be canonical.")
                if not any(
                    root in file.parents
                    for root in (settings.home / "sessions", settings.home / "archived_sessions")
                ):
                    raise CleanupError("Manifest rollout escapes the Codex session directories.")
        rebuilt = build_plan(settings, snapshot)
        if rebuilt.groups != payload["groups"] or rebuilt.holds != payload["holds"]:
            raise CleanupError("Manifest policy/groups do not match its snapshot.")
        return CleanupPlan(settings, snapshot, rebuilt.groups, rebuilt.holds, payload["created_at"])
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise CleanupError("Malformed manifest.") from error


def journal_path(manifest):
    return Path(str(Path(manifest).expanduser().absolute()) + ".journal.jsonl")


def events(path):
    path = Path(path)
    if not path.exists():
        return []
    fd = private_fd(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise InterruptedRun("Oversized journal; manual review required.")
    if raw and not raw.endswith(b"\n"):
        raise InterruptedRun("Incomplete journal record; no automatic recovery.")
    try:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    except ValueError:
        raise InterruptedRun("Malformed journal; no automatic recovery.") from None


def append_event(path, value):
    private_directory(Path(path).parent)
    fd = private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(Path(path).parent)


def journal_state(plan, records):
    identity = digest(plan.to_dict())
    by_root = {group["root"]: group for group in plan.groups}
    pending, done = None, set()
    if records and (not isinstance(records[0], dict) or records[0].get("kind") != "start"):
        raise InterruptedRun("Journal must begin with a start record.")
    for position, event in enumerate(records):
        if not isinstance(event, dict):
            raise InterruptedRun("Malformed journal record.")
        if event.get("manifest") != identity:
            raise InterruptedRun("Journal belongs to a different manifest.")
        kind = event.get("kind")
        if kind == "start":
            if position != 0 or set(event) != {"kind", "manifest"}:
                raise InterruptedRun("Unexpected journal start.")
        elif kind == "intent":
            root = event.get("root")
            if (
                set(event) != {"kind", "manifest", "root", "ids"}
                or not isinstance(root, str)
                or pending is not None
                or root not in by_root
                or root in done
            ):
                raise InterruptedRun("Invalid or duplicate deletion intent.")
            next_root = next((g["root"] for g in plan.groups if g["root"] not in done), None)
            if root != next_root:
                raise InterruptedRun("Journal requests are not in approved dependency order.")
            if event.get("ids") != by_root[root]["ids"]:
                raise InterruptedRun("Journal intent disagrees with the approved group.")
            pending = root
        elif kind == "complete":
            if (
                set(event) != {"kind", "manifest", "root"}
                or pending is None
                or event.get("root") != pending
            ):
                raise InterruptedRun("Completion without a matching intent.")
            done.add(pending)
            pending = None
        else:
            raise InterruptedRun("Unknown journal record.")
    if pending is not None:
        raise InterruptedRun(
            "An interrupted request is unresolved. Verify and rescan; do not blindly retry."
        )
    deleted = {tid for root in done for tid in by_root[root]["ids"]}
    return done, deleted


@contextmanager
def apply_lock(settings):
    if os.name != "posix":
        raise CleanupError("Apply locking requires a POSIX platform.")
    import fcntl

    # One per-user lock across all state/home choices, including shared SQLite homes.
    directory = private_directory(Path("/tmp") / f"codex-cleanup-{os.getuid()}")
    path = directory / "apply.lock"
    fd = private_fd(path, os.O_WRONLY | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CleanupError("Another cleanup run holds the home lock.") from None
        yield
    finally:
        os.close(fd)
