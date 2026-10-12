"""Pure graph policy. Edges point from a required parent to its child."""

from __future__ import annotations

import heapq
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .errors import CleanupError
from .models import CleanupPlan


def build_plan(settings, snapshot):
    rows, files = snapshot["threads"], snapshot["files"]
    refs, spawn = snapshot["references"], snapshot["spawn"]
    all_ids = set(rows) | set(files) | set(refs)
    parents = defaultdict(set)
    spawn_parents = defaultdict(set)
    for parent, children in refs.items():
        all_ids.update(children)
        for child in children:
            parents[child].add(parent)
    for parent, children in spawn.items():
        for child in children:
            spawn_parents[child].add(parent)
    reasons = defaultdict(set)
    activity = set(snapshot["activity"])
    unknown_age = set(snapshot["unknown_age"])
    eligible = set()
    for tid in all_ids:
        row, rollouts = rows.get(tid), files.get(tid, [])
        if row is None or not row["archived"]:
            reasons[tid].add("missing metadata or unarchived")
        if row and (row["pinned"] or row["daybreak"]):
            reasons[tid].add("pinned or background-enabled")
        if not rollouts or not all(f["archived"] for f in rollouts):
            reasons[tid].add("missing or non-archived rollout")
        if len(rollouts) > 1:
            reasons[tid].add("multiple logical rollout files")
        if tid in unknown_age or snapshot["last_activity"].get(tid, 0) <= 0:
            reasons[tid].add("unknown last activity")
        elif snapshot["last_activity"][tid] >= settings.cutoff.timestamp():
            reasons[tid].add("recent activity")
        if tid in activity:
            reasons[tid].add("active, queued, pinned, or nonterminal goal")
        if tid in settings.keep_ids:
            reasons[tid].add("explicit keep")
        if row:
            cwd = Path(row["resolved_cwd"])
            if any(cwd == Path(p) or Path(p) in cwd.parents for p in settings.keep_cwds):
                reasons[tid].add("excluded workspace")
        if not reasons[tid]:
            eligible.add(tid)
    protected = all_ids - eligible
    pending = list(protected)
    while pending:
        for parent in parents[pending.pop()]:
            if parent not in protected:
                protected.add(parent)
                reasons[parent].add("required ancestor of survivor")
                pending.append(parent)
    eligible -= protected
    # A cycle and everything depending on it must survive; propagate that hold
    # back to every required ancestor before computing deletion subtrees.
    degree = {tid: len(spawn_parents[tid] & eligible) for tid in eligible}
    queue = [tid for tid in eligible if degree[tid] == 0]
    visited = set()
    while queue:
        tid = queue.pop()
        visited.add(tid)
        for child in spawn.get(tid, ()):
            if child in degree:
                degree[child] -= 1
                if degree[child] == 0:
                    queue.append(child)
    cyclic = eligible - visited
    pending = list(cyclic)
    for tid in cyclic:
        reasons[tid].add("spawn cycle or dependent")
    protected.update(cyclic)
    while pending:
        for parent in parents[pending.pop()]:
            if parent not in protected:
                protected.add(parent)
                reasons[parent].add("required ancestor of survivor")
                pending.append(parent)
    eligible -= protected
    groups, owner = {}, {}
    for root in sorted(eligible):
        if spawn_parents[root] & eligible:
            continue
        members, todo = set(), [root]
        while todo:
            tid = todo.pop()
            if tid in members:
                continue
            members.add(tid)
            todo.extend(spawn.get(tid, ()))
        if not members <= eligible:
            raise CleanupError("Cascade crosses a protected boundary.")
        for tid in members:
            if tid in owner:
                raise CleanupError("Overlapping spawn subtrees.")
            owner[tid] = root
        groups[root] = {
            "root": root,
            "ids": sorted(members),
            "bytes": sum(f["bytes"] for tid in members for f in files[tid]),
        }
    for tid in eligible - set(owner):
        reasons[tid].add("spawn cycle")
    dependencies, dependents = {}, defaultdict(set)
    for root, group in groups.items():
        dependencies[root] = {
            owner[child]
            for tid in group["ids"]
            for child in refs.get(tid, ())
            if child in owner and owner[child] != root
        }
        for child in dependencies[root]:
            dependents[child].add(root)
    ready = [(-groups[root]["bytes"], root) for root in groups if not dependencies[root]]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, root = heapq.heappop(ready)
        ordered.append(groups[root])
        for parent in dependents[root]:
            dependencies[parent].remove(root)
            if not dependencies[parent]:
                heapq.heappush(ready, (-groups[parent]["bytes"], parent))
    targets = {tid for group in ordered for tid in group["ids"]}
    for root in set(groups) - {group["root"] for group in ordered}:
        for tid in groups[root]["ids"]:
            reasons[tid].add("dependency cycle")
    for tid in targets:
        if any(child not in targets for child in refs.get(tid, ())):
            raise CleanupError("A survivor references a proposed deletion.")
    archives = {tid for tid, rollouts in files.items() if all(f["archived"] for f in rollouts)}
    holds = {tid: sorted(reasons[tid]) for tid in sorted(archives - targets)}
    return CleanupPlan(settings, snapshot, ordered, holds, datetime.now(timezone.utc).isoformat())
