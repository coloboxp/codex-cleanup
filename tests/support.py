"""All writable stores are newly created temporary fixtures."""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from codex_cleanup import Settings

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
CUTOFF = datetime(2021, 1, 1, tzinfo=timezone.utc)


def tid(number):
    return str(uuid.UUID(int=number))


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / "codex"
        self.home.mkdir(parents=True)
        (self.home / ".synthetic-store").touch()
        (self.home / "archived_sessions").mkdir()
        (self.home / "sessions").mkdir()
        self.state = self.root / "private"
        self.settings = Settings(self.home, self.home, CUTOFF, state_dir=self.state)
        self.sql(
            "state_5.sqlite",
            """CREATE TABLE threads (
            id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, archived INTEGER,
            is_pinned INTEGER, daybreak_enabled INTEGER, created_at INTEGER,
            updated_at INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER, source TEXT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);""",
        )
        self.sql(
            "thread_history_1.sqlite",
            """CREATE TABLE thread_turns(
            thread_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER,
            rollout_ordinal INTEGER);""",
        )
        self.sql("goals_1.sqlite", "CREATE TABLE thread_goals(thread_id TEXT,status TEXT);")
        self.sql("queue_1.sqlite", "CREATE TABLE queued_items(thread_id TEXT);")
        (self.home / ".codex-global-state.json").write_text("{}")
        (self.home / "session_index.jsonl").touch()
        self.binary = self.root / "fake-codex"
        source = (Path(__file__).parent / "fake_server.py").read_text()
        self.binary.write_text("#!" + sys.executable + "\n" + source)
        self.binary.chmod(0o700)

    def sql(self, name, script, params=None):
        db = sqlite3.connect(self.home / name)
        try:
            if params is None:
                db.executescript(script)
            else:
                db.execute(script, params)
            db.commit()
        finally:
            db.close()

    def add(
        self,
        number,
        *,
        archived=True,
        recent=False,
        pinned=False,
        background=0,
        parent=None,
        size=0,
    ):
        identity = tid(number)
        date = datetime.now(timezone.utc).timestamp() if recent else OLD
        directory = self.home / ("archived_sessions" if archived else "sessions")
        path = directory / ("rollout-" + identity + ".jsonl")
        payload = {
            "id": identity,
            "timestamp": datetime.fromtimestamp(date, timezone.utc).isoformat(),
        }
        if parent:
            payload["forked_from_id"] = tid(parent)
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload})
            + "\n"
            + json.dumps({"type": "synthetic_turn", "text": "PRIVATE_BODY_SENTINEL" + "x" * size})
            + "\n"
        )
        os.utime(path, (date, date))
        self.sql(
            "state_5.sqlite",
            "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                str(path),
                "/workspace/example",
                int(archived),
                int(pinned),
                background,
                int(date),
                int(date),
                int(date * 1000),
                int(date * 1000),
                '"cli"',
            ),
        )
        with (self.home / "session_index.jsonl").open("a") as handle:
            handle.write(
                json.dumps({"id": identity, "thread_name": "PRIVATE_TITLE_SENTINEL"}) + "\n"
            )
        return path

    def spawn(self, parent, child):
        self.sql(
            "state_5.sqlite",
            "INSERT INTO thread_spawn_edges VALUES (?,?)",
            (tid(parent), tid(child)),
        )

    def manifest(self):
        from codex_cleanup import scan, write_manifest

        return write_manifest(scan(self.settings), self.state / "review.json")
