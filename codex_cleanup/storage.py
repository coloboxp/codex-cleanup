"""Read-only SQLite and bounded header inspection; never reads turn text."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .errors import StalePlan, UnsupportedLayout
from .models import thread_id

MAX_JSON = 16 * 1024**2
MAX_HEADER = 256 * 1024
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
STATE_COLUMNS = {
    "id",
    "rollout_path",
    "cwd",
    "archived",
    "is_pinned",
    "daybreak_enabled",
    "created_at",
    "updated_at",
    "created_at_ms",
    "updated_at_ms",
    "source",
    "model_provider",
    "title",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "has_user_event",
    "archived_at",
    "git_sha",
    "git_branch",
    "git_origin_url",
    "cli_version",
    "first_user_message",
    "agent_nickname",
    "agent_role",
    "memory_mode",
    "model",
    "reasoning_effort",
    "agent_path",
    "thread_source",
    "preview",
    "recency_at",
    "recency_at_ms",
    "history_mode",
    "name",
    "thread_section_id",
    "section_position",
    "section_entered_at_ms",
    "project_id",
    "originator",
    "creator_user_id",
    "creator_account_id",
}
CORE_COLUMNS = {
    "id",
    "rollout_path",
    "cwd",
    "archived",
    "is_pinned",
    "daybreak_enabled",
    "source",
    "created_at",
    "updated_at",
    "created_at_ms",
    "updated_at_ms",
}


@contextmanager
def readonly(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise UnsupportedLayout("Required SQLite store is absent or symlinked.")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def columns(db, table, required):
    found = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
    if not required <= found:
        raise UnsupportedLayout(f"Unsupported {table} schema.")
    return found


def layout(settings):
    expected = {"state": 5, "thread_history": 1, "goals": 1, "queue": 1}
    for prefix, version in expected.items():
        candidates = []
        for path in settings.sqlite_home.glob(f"{prefix}_*.sqlite"):
            match = re.fullmatch(re.escape(prefix) + r"_(\d+)\.sqlite", path.name)
            if match:
                candidates.append(int(match[1]))
        if not candidates or max(candidates) != version:
            raise UnsupportedLayout(f"Unsupported or missing {prefix} store version.")


def metadata(settings):
    layout(settings)
    with readonly(settings.sqlite_home / "state_5.sqlite") as db:
        found = columns(db, "threads", CORE_COLUMNS)
        if found - STATE_COLUMNS:
            raise UnsupportedLayout("Unreviewed thread columns; refuse to ignore new safety flags.")
        background = "daybreak_enabled" if "daybreak_enabled" in found else "0"
        source = "source" if "source" in found else "NULL"
        additional_dates = [
            name for name in ("archived_at", "recency_at", "recency_at_ms") if name in found
        ]
        extra = "".join("," + name for name in additional_dates)
        result = {}
        query = f"""SELECT id,rollout_path,cwd,archived,is_pinned,{background},
                    created_at,updated_at,created_at_ms,updated_at_ms,{source}{extra} FROM threads"""
        for row in db.execute(query):
            tid = thread_id(row[0])
            if row[3] not in (0, 1) or row[4] not in (0, 1) or row[5] not in (0, 1, None):
                raise UnsupportedLayout("Unknown archive/pin/background flags.")
            if not isinstance(row[1], str) or not isinstance(row[2], str):
                raise UnsupportedLayout("Missing rollout/workspace path.")
            path = Path(row[1])
            if not path.is_absolute() or path != path.resolve():
                raise UnsupportedLayout("Backend rollout path is not canonical.")
            root = settings.home / ("archived_sessions" if row[3] else "sessions")
            if root not in path.parents:
                raise UnsupportedLayout(
                    "Backend rollout is outside its reviewed session directory."
                )
            if not Path(row[2]).is_absolute():
                raise UnsupportedLayout("Workspace path must be absolute.")
            result[tid] = {
                "path": row[1],
                "cwd": row[2],
                "archived": row[3],
                "resolved_cwd": str(Path(row[2]).resolve()),
                "pinned": row[4],
                "daybreak": row[5] != 0,
                "dates": list(row[6:10]) + list(row[11:]),
                "source_refs": source_references(row[10]),
            }
        columns(db, "thread_spawn_edges", {"parent_thread_id", "child_thread_id"})
        spawn = defaultdict(set)
        for parent, child in db.execute(
            "SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"
        ):
            spawn[thread_id(parent)].add(thread_id(child))
    return result, {p: sorted(children) for p, children in spawn.items()}


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 1e14:
                number /= 1e6
            elif number > 1e11:
                number /= 1000
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            number = parsed.timestamp()
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def ids_in(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(UUID_RE.findall(key))
            result.update(ids_in(item))
    elif isinstance(value, list):
        for item in value:
            result.update(ids_in(item))
    elif isinstance(value, str):
        result.update(UUID_RE.findall(value))
    return result


def source_references(value):
    reference_free = {"cli", "vscode", "exec", "app-server", "unknown"}
    if value is None:
        return []
    if isinstance(value, str):
        if value in reference_free:
            return []
        try:
            value = json.loads(value)
        except ValueError:
            raise UnsupportedLayout("Malformed source metadata; ancestry is uncertain.") from None
        if isinstance(value, str) and value in reference_free:
            return []
    result = set()
    allowed_keys = {
        "subagent",
        "thread_spawn",
        "review",
        "compact",
        "other",
        "memory_consolidation",
        "parent_thread_id",
        "parentThreadId",
        "depth",
        "agent_nickname",
        "agent_role",
        "agent_path",
        "nickname",
        "role",
    }
    if isinstance(value, dict):
        if set(value) - allowed_keys:
            raise UnsupportedLayout("Unreviewed source relationship fields.")
        for key, item in value.items():
            if key in ("parent_thread_id", "parentThreadId"):
                result.add(thread_id(item))
            elif isinstance(item, (dict, list)):
                result.update(source_references(item))
            elif isinstance(item, str) and UUID_RE.search(item):
                raise UnsupportedLayout("Unreviewed UUID-valued source field.")
    elif isinstance(value, list):
        for item in value:
            result.update(source_references(item))
    else:
        raise UnsupportedLayout("Unreviewed source metadata.")
    return sorted(result)


def json_file(path, optional=False):
    if optional and not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > MAX_JSON:
        raise UnsupportedLayout("Unsafe or oversized JSON metadata file.")
    with path.open("rb") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise UnsupportedLayout("JSON metadata must be an object.")
    return value


def activity(settings):
    result = set()
    global_state = json_file(settings.home / ".codex-global-state.json", optional=True)
    keys = (
        "pinned-thread-ids",
        "queued-follow-ups",
        "opaqueWindows",
        "active-thread-id",
        "active-thread-ids",
        "open-thread-ids",
    )
    for key in keys:
        result.update(ids_in(global_state.get(key, {})))
    atom = global_state.get("electron-persisted-atom-state", {})
    if not isinstance(atom, dict):
        raise UnsupportedLayout("Unknown desktop activity state.")
    for key in (
        "client-thread-bindings-v1",
        "active-thread-id",
        "active-thread-ids",
        "open-thread-ids",
        "selected-thread-id",
    ):
        result.update(ids_in(atom.get(key, {})))
    with readonly(settings.sqlite_home / "goals_1.sqlite") as db:
        columns(db, "thread_goals", {"thread_id", "status"})
        for tid, status in db.execute("SELECT thread_id,status FROM thread_goals"):
            if status not in ("complete", "completed", "cancelled", "failed"):
                result.add(thread_id(tid))
    with readonly(settings.sqlite_home / "queue_1.sqlite") as db:
        columns(db, "queued_items", {"thread_id"})
        result.update(
            thread_id(row[0]) for row in db.execute("SELECT DISTINCT thread_id FROM queued_items")
        )
    return result


def file_record(path, archived, raw):
    stat = path.stat()
    return {
        "path": str(path),
        "archived": archived,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "header_sha256": hashlib.sha256(raw).hexdigest(),
    }


def check_file(record):
    path = Path(record["path"])
    if path.is_symlink() or not path.is_file():
        raise StalePlan("A scanned rollout is absent or symlinked.")
    with path.open("rb") as handle:
        raw = handle.readline(MAX_HEADER + 1)
    if file_record(path, record["archived"], raw) != record:
        raise StalePlan("A scanned rollout changed.")


def snapshot(settings):
    rows, spawn = metadata(settings)
    support = supporting_records(settings)
    index = index_records(settings)
    desktop = desktop_hash(settings)
    by_path = {}
    for tid, row in rows.items():
        if row["path"] in by_path:
            raise UnsupportedLayout("Ambiguous rollout ownership.")
        if row["path"]:
            by_path[row["path"]] = tid
    files, references = defaultdict(list), defaultdict(set)
    last, unknown = {}, set()
    for tid, row in rows.items():
        dates = [timestamp(v) for v in row["dates"]]
        if not any(dates) or any(v is not None and timestamp(v) is None for v in row["dates"]):
            unknown.add(tid)
        last[tid] = max((v for v in dates if v is not None), default=0)
        for parent in row["source_refs"]:
            if parent != tid:
                references[parent].add(tid)
    for parent, children in spawn.items():
        references[parent].update(child for child in children if child != parent)
    for directory in (settings.home / "sessions", settings.home / "archived_sessions"):
        if directory.is_symlink():
            raise UnsupportedLayout("Symlinked session directories need separate review.")
        for path in directory.rglob("*"):
            if path.is_symlink() or path.name.endswith(".jsonl.zst"):
                raise UnsupportedLayout("Symlinked or compressed rollouts need separate review.")
            if not path.is_file() or not path.name.endswith(".jsonl"):
                continue
            before = path.stat()
            with path.open("rb") as handle:
                raw = handle.readline(MAX_HEADER + 1)
            if len(raw) > MAX_HEADER or not raw.endswith(b"\n"):
                raise UnsupportedLayout("Incomplete or oversized rollout header.")
            try:
                envelope = json.loads(raw)
                if not isinstance(envelope, dict) or envelope.get("type") != "session_meta":
                    raise UnsupportedLayout(
                        "First rollout record must be reviewed session metadata."
                    )
                header = envelope["payload"]
                tid = thread_id(header["id"])
            except (ValueError, KeyError, TypeError):
                raise UnsupportedLayout("Unreadable rollout identity.") from None
            if str(path) in by_path and by_path[str(path)] != tid:
                raise UnsupportedLayout("Conflicting logical rollout identity.")
            record = file_record(path, path.parent == settings.home / "archived_sessions", raw)
            if before.st_mtime_ns != record["mtime_ns"] or before.st_size != record["bytes"]:
                raise StalePlan("A rollout changed during scanning.")
            files[tid].append(record)
            date = timestamp(header.get("timestamp"))
            if date is None:
                unknown.add(tid)
            last[tid] = max(last.get(tid, 0), date or 0, before.st_mtime)
            refs = source_references(header.get("source"))
            if header.get("forked_from_id"):
                refs.append(thread_id(header["forked_from_id"]))
            if header.get("history_base") is not None:
                history_base = header["history_base"]
                if not isinstance(history_base, dict) or "thread_id" not in history_base:
                    raise UnsupportedLayout("Unknown history-base format.")
                refs.append(thread_id(history_base["thread_id"]))
            for parent in refs:
                if parent != tid:
                    references[parent].add(tid)
    initial_activity = activity(settings)
    active = set(initial_activity)
    latest = {}
    with readonly(settings.sqlite_home / "thread_history_1.sqlite") as db:
        columns(
            db,
            "thread_turns",
            {"thread_id", "status", "started_at", "completed_at", "rollout_ordinal"},
        )
        for tid, status, start, end, ordinal in db.execute(
            "SELECT thread_id,status,started_at,completed_at,rollout_ordinal FROM thread_turns"
        ):
            thread_id(tid)
            for value in (start, end):
                parsed = timestamp(value)
                if value is not None and parsed is None:
                    unknown.add(tid)
                last[tid] = max(last.get(tid, 0), parsed or 0)
            if not isinstance(ordinal, int):
                raise UnsupportedLayout("Unknown turn ordering.")
            if tid not in latest or ordinal > latest[tid][0]:
                latest[tid] = (ordinal, status)
            elif ordinal == latest[tid][0]:
                raise UnsupportedLayout("Ambiguous duplicate latest-turn ordering.")
    terminal = {"completed", "interrupted", "cancelled", "failed", "aborted", "errored"}
    active.update(tid for tid, (_, status) in latest.items() if status not in terminal)
    end_rows, end_spawn = metadata(settings)
    if rows != end_rows or spawn != end_spawn:
        raise StalePlan("Metadata changed during the scan; close Codex and rescan.")
    if activity(settings) != initial_activity:
        raise StalePlan("Desktop or queue activity changed during the scan.")
    for tid, row in rows.items():
        if not files.get(tid):
            raise UnsupportedLayout(
                "A metadata rollout is missing; its ancestry cannot be inspected."
            )
        if not any(f["path"] == row["path"] for f in files[tid]):
            raise UnsupportedLayout("Metadata does not own the scanned rollout.")
    for records in files.values():
        for record in records:
            check_file(record)
    if (
        supporting_records(settings) != support
        or index_records(settings) != index
        or desktop_hash(settings) != desktop
    ):
        raise StalePlan("Supporting metadata changed during the scan.")
    return {
        "threads": rows,
        "files": {k: sorted(v, key=lambda f: f["path"]) for k, v in files.items()},
        "references": {k: sorted(v) for k, v in references.items()},
        "spawn": spawn,
        "activity": sorted(active),
        "last_activity": last,
        "unknown_age": sorted(unknown),
        "support": support,
        "index": index,
        "desktop_hash": desktop,
    }


def desktop_hash(settings):
    value = json_file(settings.home / ".codex-global-state.json", optional=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def supporting_records(settings):
    result = {}
    for key, name, table, selection in (
        (
            "history",
            "thread_history_1.sqlite",
            "thread_turns",
            "thread_id,status,started_at,completed_at,rollout_ordinal",
        ),
        ("goals", "goals_1.sqlite", "thread_goals", "thread_id,status"),
        ("queue", "queue_1.sqlite", "queued_items", "thread_id"),
    ):
        with readonly(settings.sqlite_home / name) as db:
            rows = [list(row) for row in db.execute(f"SELECT {selection} FROM {table}")]
            result[key] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise UnsupportedLayout("SQLite integrity verification failed.")
    with readonly(settings.sqlite_home / "state_5.sqlite") as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise UnsupportedLayout("SQLite integrity verification failed.")
    return result


def index_records(settings):
    path = settings.home / "session_index.jsonl"
    if not path.exists():
        return {}
    if path.is_symlink():
        raise UnsupportedLayout("Session index must not be symlinked.")
    result = defaultdict(list)
    with path.open("rb") as handle:
        while True:
            raw = handle.readline(MAX_HEADER + 1)
            if not raw:
                break
            if len(raw) > MAX_HEADER or not raw.endswith(b"\n"):
                raise UnsupportedLayout("Incomplete or oversized session-index record.")
            try:
                record = json.loads(raw)
                tid = thread_id(record["id"])
            except (ValueError, KeyError, TypeError):
                raise UnsupportedLayout("Unreviewed session-index format.") from None
            # Fingerprint names without storing them in the manifest.
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            result[tid].append(hashlib.sha256(canonical).hexdigest())
    return {tid: sorted(values) for tid, values in result.items()}


def without_deleted(value, deleted):
    result = {
        key: {tid: row for tid, row in value[key].items() if tid not in deleted}
        for key in ("threads", "files", "last_activity", "index")
    }
    for key in ("references", "spawn"):
        result[key] = {
            parent: [child for child in children if child not in deleted]
            for parent, children in value[key].items()
            if parent not in deleted and any(child not in deleted for child in children)
        }
    for key in ("activity", "unknown_age"):
        result[key] = [tid for tid in value[key] if tid not in deleted]
    result["support"] = {
        key: [row for row in rows if row[0] not in deleted]
        for key, rows in value["support"].items()
    }
    result["desktop_hash"] = value["desktop_hash"]
    return result
