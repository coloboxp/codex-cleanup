"""Read-only diagnostics and conservative compatibility gates."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import UnsafeRuntime

REVIEWED_VERSIONS = frozenset({"0.162.0", "0.162.1"})


@dataclass(frozen=True)
class Runtime:
    binary: Path
    version: str


def discover(settings, binary=None):
    if binary:
        path = Path(binary).expanduser().resolve()
    else:
        candidates = [
            settings.home / "packages/app-server-daemon/current/bin/codex",
            Path(shutil.which("codex") or "/nonexistent-codex-runtime"),
        ]
        path = next((p.resolve() for p in candidates if p.is_file()), None)
    if path is None or not path.is_file():
        raise UnsafeRuntime("No local Codex binary found; specify --codex-bin.")
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15)
    match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)\s*", result.stdout)
    if result.returncode or not match:
        raise UnsafeRuntime("Cannot verify the Codex runtime version.")
    return Runtime(path, match.group(1))


def require_reviewed(runtime):
    if runtime.version not in REVIEWED_VERSIONS:
        raise UnsafeRuntime(
            f"Codex {runtime.version} is unreviewed. Apply supports only "
            + ", ".join(sorted(REVIEWED_VERSIONS))
            + "; there is no unsafe version bypass."
        )


def fd_paths(pid):
    if sys.platform.startswith("linux"):
        directory = Path(f"/proc/{pid}/fd")
        if not directory.is_dir():
            raise UnsafeRuntime("Owned app-server is not running.")
        result = set()
        for item in directory.iterdir():
            try:
                target = os.readlink(item)
            except FileNotFoundError:
                continue
            if target.startswith("/"):
                result.add(Path(target))
        return result
    if sys.platform == "darwin":
        executable = shutil.which("lsof")
        if not executable:
            raise UnsafeRuntime("lsof is required for store verification.")
        result = subprocess.run(
            [executable, "-nP", "-a", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode:
            raise UnsafeRuntime("Cannot inspect the owned server's open files.")
        return {Path(line[1:]) for line in result.stdout.splitlines() if line.startswith("n/")}
    raise UnsafeRuntime("Apply supports macOS and Linux only.")


def require_private_store(settings):
    paths = {settings.home, settings.sqlite_home}
    paths.update(settings.sqlite_home.glob("*.sqlite*"))
    for directory in (settings.home / "sessions", settings.home / "archived_sessions"):
        if directory.exists():
            paths.add(directory)
            paths.update(directory.rglob("*"))
    for filename in (".codex-global-state.json", "session_index.jsonl"):
        path = settings.home / filename
        if path.exists():
            paths.add(path)
    for path in paths:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise UnsafeRuntime("Apply requires owned, non-symlinked, non-shared-writable stores.")
    for root in (settings.home, settings.sqlite_home):
        for parent in root.parents:
            info = parent.stat()
            if info.st_mode & 0o022 and not info.st_mode & 0o1000:
                raise UnsafeRuntime(
                    "A writable ancestor directory makes store ownership unverifiable."
                )


def foreign_writable_files(settings, owned_pid):
    ignored = {os.getpid(), owned_pid}
    roots = (settings.home, settings.sqlite_home)

    def relevant(name):
        path = Path(name.removesuffix(" (deleted)"))
        return path.is_absolute() and any(root == path or root in path.parents for root in roots)

    if sys.platform == "darwin":
        executable = shutil.which("lsof")
        if not executable:
            raise UnsafeRuntime("lsof is required for writer inspection.")
        result = subprocess.run(
            [executable, "-nP", "-u", str(os.getuid()), "-Fpfan"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise UnsafeRuntime("Cannot inspect foreign store handles.")
        pid, access = None, None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("f"):
                access = None
            elif line.startswith("a"):
                access = line[1:]
            elif line.startswith("n/") and pid not in ignored and access in ("w", "u"):
                if relevant(line[1:]):
                    return True
        return False
    if sys.platform.startswith("linux"):
        for process in Path("/proc").iterdir():
            if not process.name.isdigit() or int(process.name) in ignored:
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                for descriptor in (process / "fd").iterdir():
                    name = os.readlink(descriptor)
                    if not relevant(name):
                        continue
                    info = (process / "fdinfo" / descriptor.name).read_text()
                    flags = next(
                        line.split()[1] for line in info.splitlines() if line.startswith("flags:")
                    )
                    if int(flags, 8) & os.O_ACCMODE:
                        return True
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise UnsafeRuntime("Cannot inspect another same-user process.") from None
        return False
    raise UnsafeRuntime("Writer inspection supports macOS and Linux only.")


class RuntimeGuard:
    def check(self, settings, owned_pid=None):
        require_private_store(settings)
        if os.name != "posix":
            raise UnsafeRuntime("Apply requires POSIX process inspection.")
        result = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        for line in result.stdout.splitlines():
            row = line.strip().split(None, 1)
            if len(row) != 2 or int(row[0]) == owned_pid:
                continue
            if Path(row[1]).name.lower() == "codex" or "/Codex.app/" in row[1]:
                raise UnsafeRuntime("Close all Codex app/CLI/daemon processes before apply.")
        if (settings.home / "app-server-control/app-server-control.sock").exists():
            raise UnsafeRuntime(
                "An existing daemon socket blocks isolated apply; do not remove it automatically."
            )
        for variable, expected in (
            ("CODEX_HOME", settings.home),
            ("CODEX_SQLITE_HOME", settings.sqlite_home),
        ):
            value = os.environ.get(variable)
            if value and Path(value).expanduser().resolve() != expected:
                raise UnsafeRuntime(f"{variable} conflicts with the reviewed manifest.")
        if foreign_writable_files(settings, owned_pid):
            raise UnsafeRuntime("Another process has writable Codex-store files open.")

    def verify_store(self, settings, pid):
        observed = {p for p in fd_paths(pid) if p.name == "state_5.sqlite"}
        if observed != {settings.sqlite_home / "state_5.sqlite"}:
            raise UnsafeRuntime("Cannot prove that the owned server uses the intended SQLite home.")


def doctor(settings, binary=None):
    result = {"reviewed_versions": sorted(REVIEWED_VERSIONS), "platform": sys.platform}
    try:
        runtime = discover(settings, binary)
        result.update(
            runtime_version=runtime.version, reviewed=runtime.version in REVIEWED_VERSIONS
        )
    except UnsafeRuntime as error:
        result.update(reviewed=False, runtime_error=str(error))
    try:
        RuntimeGuard().check(settings)
        result["quiescent"] = True
    except UnsafeRuntime as error:
        result.update(quiescent=False, process_error=str(error))
    return result
