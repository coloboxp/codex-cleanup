"""Run directly from a downloaded source directory."""

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import CleanupError, Settings, __version__, apply, doctor, scan, verify, write_manifest


def common(parser):
    parser.add_argument("--home", type=Path, help="Codex home; default CODEX_HOME or ~/.codex")
    parser.add_argument("--sqlite-home", type=Path, help="SQLite home; defaults to Codex home")
    parser.add_argument(
        "--state-dir", type=Path, help="Private tool state; default ~/.codex-cleanup"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Alpha: review and remove only old archived Codex threads."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    scanner = commands.add_parser("scan", help="Read-only inventory and private manifest")
    common(scanner)
    age = scanner.add_mutually_exclusive_group()
    age.add_argument("--days", type=int, default=30)
    age.add_argument("--before", help="Exclusive timezone-aware cutoff, e.g. 2025-01-01T00:00:00Z")
    scanner.add_argument("--keep-thread", action="append", default=[])
    scanner.add_argument("--keep-cwd", action="append", default=[])
    scanner.add_argument(
        "--out", type=Path, help="New private manifest filename; never overwritten"
    )
    applier = commands.add_parser("apply", help="Irreversible supported-API deletion")
    applier.add_argument("manifest", type=Path)
    applier.add_argument("--confirm-delete", action="store_true")
    applier.add_argument("--max-requests", type=int, default=100)
    applier.add_argument("--codex-bin", type=Path)
    verifier = commands.add_parser("verify", help="Read-only verification of all planned deletions")
    verifier.add_argument("manifest", type=Path)
    diagnostic = commands.add_parser(
        "doctor", help="Read-only runtime/process diagnostics; no app-server"
    )
    common(diagnostic)
    diagnostic.add_argument("--codex-bin", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in ("scan", "doctor"):
            extra = {"state_dir": args.state_dir} if args.state_dir else {}
            settings = Settings.default(
                home=args.home,
                sqlite_home=args.sqlite_home,
                days=getattr(args, "days", 30),
                before=getattr(args, "before", None),
                keep_ids=getattr(args, "keep_thread", []),
                keep_cwds=getattr(args, "keep_cwd", []),
                **extra,
            )
        if args.command == "scan":
            plan = scan(settings)
            out = args.out or settings.state_dir / (
                "scan-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
            )
            filename = write_manifest(plan, out)
            result = dict(plan.summary(), manifest=str(filename), read_only_scan=True)
        elif args.command == "apply":
            result = apply(
                args.manifest,
                confirm=args.confirm_delete,
                max_requests=args.max_requests,
                binary=args.codex_bin,
            )
        elif args.command == "verify":
            result = verify(args.manifest)
        else:
            result = doctor(settings, args.codex_bin)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except CleanupError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        # Avoid dumping backend output or a traceback with private local data.
        print(f"Refused: local operation failed ({type(error).__name__}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
