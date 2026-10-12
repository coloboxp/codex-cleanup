"""Executable fixture only: refuses stores without an explicit synthetic marker."""

import json
import os
import sqlite3
import sys
from pathlib import Path


def main():
    if "--version" in sys.argv:
        print("codex-cli " + os.environ.get("FAKE_VERSION", "0.162.1"))
        return
    home = Path(os.environ["CODEX_HOME"])
    sql = Path(os.environ.get("FAKE_STORE", os.environ["CODEX_SQLITE_HOME"]))
    if not (home / ".synthetic-store").is_file() or not (sql / ".synthetic-store").is_file():
        raise RuntimeError("Fake runtime accepts only marked synthetic fixtures.")
    db = sqlite3.connect(sql / "state_5.sqlite")
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        frame = os.environ.get("FAKE_FRAME")
        if frame:
            value = {"id": request["id"], "result": {}}
            if frame == "request":
                value["method"] = "synthetic/approval"
            elif frame == "boolean-id":
                value["id"] = True
            elif frame == "float-id":
                value["id"] = float(request["id"])
            print(json.dumps(value), flush=True)
            continue
        result = {}
        if method == "thread/loaded/list":
            result = {"data": [str(1)] if os.environ.get("FAKE_LOADED") else [], "nextCursor": None}
        elif method == "thread/delete":
            root = request["params"]["threadId"]
            with (home / ".requests").open("a") as handle:
                handle.write(root + "\n")
            if os.environ.get("FAKE_ERROR"):
                print(
                    json.dumps(
                        {"id": request["id"], "error": {"message": "PRIVATE_ERROR_SENTINEL"}}
                    ),
                    flush=True,
                )
                continue
            ids, todo = set(), [root]
            while todo:
                identity = todo.pop()
                if identity in ids:
                    continue
                ids.add(identity)
                todo.extend(
                    row[0]
                    for row in db.execute(
                        "SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id=?",
                        (identity,),
                    )
                )
            if os.environ.get("FAKE_OVERDELETE"):
                ids.update(row[0] for row in db.execute("SELECT id FROM threads"))
            # Writable SQL and unlink exist ONLY inside this marked test fixture.
            for identity in ids:
                path = db.execute(
                    "SELECT rollout_path FROM threads WHERE id=?", (identity,)
                ).fetchone()
                if path:
                    candidate = Path(path[0])
                    if home not in candidate.parents:
                        raise RuntimeError("Fixture path escapes synthetic home.")
                    candidate.unlink()
                db.execute("DELETE FROM threads WHERE id=?", (identity,))
                db.execute(
                    "DELETE FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?",
                    (identity, identity),
                )
                for name, table in (
                    ("thread_history_1.sqlite", "thread_turns"),
                    ("goals_1.sqlite", "thread_goals"),
                    ("queue_1.sqlite", "queued_items"),
                ):
                    with sqlite3.connect(sql / name) as auxiliary:
                        auxiliary.execute(f"DELETE FROM {table} WHERE thread_id=?", (identity,))
            db.commit()
            index = home / "session_index.jsonl"
            index.write_text(
                "".join(
                    line
                    for line in index.read_text().splitlines(keepends=True)
                    if json.loads(line)["id"] not in ids
                )
            )
            if os.environ.get("FAKE_CRASH"):
                return
        elif method != "initialize":
            raise RuntimeError("Unexpected fixture method.")
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
    db.close()


if __name__ == "__main__":
    main()
