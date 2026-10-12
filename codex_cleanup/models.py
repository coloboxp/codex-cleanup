"""Public configuration and result types; no machine-specific defaults."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import CleanupError

VERSION = "0.1.0a2"


def thread_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise CleanupError("Invalid or noncanonical thread UUID.") from None
    return value


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CleanupError("Cutoff must be a timezone-aware date/time.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Settings:
    home: Path
    sqlite_home: Path
    cutoff: datetime
    keep_ids: tuple = ()
    keep_cwds: tuple = ()
    state_dir: Path = field(default_factory=lambda: Path.home() / ".codex-cleanup")

    def __post_init__(self):
        for key in ("home", "sqlite_home", "state_dir"):
            object.__setattr__(self, key, Path(getattr(self, key)).expanduser().resolve())
        object.__setattr__(self, "cutoff", utc(self.cutoff))
        if self.cutoff > datetime.now(timezone.utc):
            raise CleanupError("Cutoff must not be in the future.")
        object.__setattr__(self, "keep_ids", tuple(thread_id(v) for v in self.keep_ids))
        object.__setattr__(
            self, "keep_cwds", tuple(str(Path(v).expanduser().resolve()) for v in self.keep_cwds)
        )
        if any(
            self.state_dir == p or p in self.state_dir.parents
            for p in (self.home, self.sqlite_home)
        ):
            raise CleanupError("Tool state must be outside the Codex and SQLite homes.")

    @classmethod
    def default(cls, *, days=30, before=None, home=None, sqlite_home=None, **kwargs):
        if days < 1:
            raise CleanupError("Age must be at least one day.")
        home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        sql = Path(sqlite_home or os.environ.get("CODEX_SQLITE_HOME") or home)
        cutoff = utc(before) if before else datetime.now(timezone.utc) - timedelta(days=days)
        return cls(home, sql, cutoff, **kwargs)

    def to_dict(self):
        return {
            "home": str(self.home),
            "sqlite_home": str(self.sqlite_home),
            "cutoff": self.cutoff.isoformat(),
            "keep_ids": list(self.keep_ids),
            "keep_cwds": list(self.keep_cwds),
            "state_dir": str(self.state_dir),
        }

    @classmethod
    def from_dict(cls, value):
        if set(value) != {"home", "sqlite_home", "cutoff", "keep_ids", "keep_cwds", "state_dir"}:
            raise CleanupError("Unknown settings fields.")
        return cls(**value)


@dataclass(frozen=True)
class CleanupPlan:
    settings: Settings
    snapshot: dict
    groups: list
    holds: dict
    created_at: str

    @property
    def target_ids(self):
        return {tid for group in self.groups for tid in group["ids"]}

    def summary(self):
        from collections import Counter

        total = sum(group["bytes"] for group in self.groups)
        return {
            "alpha_version": VERSION,
            "cutoff_utc": self.settings.cutoff.isoformat(),
            "candidate_threads": len(self.target_ids),
            "delete_requests": len(self.groups),
            "logical_bytes": total,
            "logical_gib": round(total / 1024**3, 3),
            "preserved_archives": len(self.holds),
            "preservation_reasons": dict(
                Counter(reason for reasons in self.holds.values() for reason in reasons)
            ),
        }

    def to_dict(self):
        return {
            "settings": self.settings.to_dict(),
            "snapshot": self.snapshot,
            "groups": self.groups,
            "holds": self.holds,
            "created_at": self.created_at,
        }
