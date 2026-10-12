# Library and CLI reference
The library uses Python 3.9+ and the standard library only. Run it from its source directory; no installation or build step is needed.

## Public API
- `Settings(home, sqlite_home, cutoff, keep_ids=(), keep_cwds=(), state_dir=...)`: immutable configuration. Paths resolve locally; cutoff requires a timezone and cannot be in the future. State must be outside both source homes.
- `Settings.default(days=30, before=None, home=None, sqlite_home=None, **kwargs)`: rolling cutoff and environment/home-derived defaults. `before` overrides the age calculation. Additional keyword arguments are the constructor’s keep/state options.
- `CleanupPlan`: immutable outer result container with `settings`, `snapshot`, `groups`, `holds`, `created_at`. Nested collections are not immutable; do not edit them.
- `plan.target_ids`: set of proposed thread UUIDs.
- `plan.summary()`: aggregate counts, sizes, cutoff and preservation reasons.
- `plan.to_dict()`: private JSON-compatible metadata representation.
- `scan(settings=None) -> CleanupPlan`: read-only planning; no runtime process is started.
- `write_manifest(plan, path) -> Path`: creates a new owner-only JSON manifest outside source homes; refuses overwrite.
- `load_manifest(path) -> CleanupPlan`: checks format, permissions, corruption digest, path boundaries and rebuilt policy. The digest is not authentication; only use manifests you created and reviewed.
- `doctor(settings, binary=None) -> dict`: diagnostics; runs the selected binary’s `--version` and read-only process/ownership inspection, not an app-server.
- `apply(manifest, confirm=False, max_requests=100, binary=None) -> dict`: irreversible supported-API deletion; requires `confirm=True`. Returns `verified`, cumulative `deleted_threads`, and `remaining_requests`.
- `verify(plan_or_manifest, deleted=None) -> dict`: read-only verification; by default expects every target absent. Library callers may pass a subset of approved IDs for partial verification. Returns `verified` and `deleted_threads`.
- `CleanupError`: base for controlled refusals. Local filesystem, SQLite or subprocess errors may also propagate to library callers; never automatically retry after an apply error.

`VERSION` / `__version__` identify the alpha version. Internal module functions are not a stable API.

## Command behavior
Run `python3 -m codex_cleanup COMMAND --help` for exact options.

- `scan`: JSON summary and manifest path. Creating the private manifest is its only intentional write.
- `apply MANIFEST --confirm-delete`: bounded deletion. Successful completed batches can continue using the unchanged manifest and journal.
- `verify MANIFEST`: checks the complete plan, not just a previous bounded batch.
- `doctor`: JSON diagnostics; inspect `reviewed` and `quiescent`, not just its exit status.

CLI exit status is 0 on successful execution/diagnostic reporting, 2 for a controlled refusal or invalid arguments. A keyboard interrupt may use the interpreter’s interruption status. All apply failures warrant review; the on-disk journal is the authority for whether a request was sent.

## Private artifacts
Manifest and adjacent `MANIFEST.journal.jsonl` files use mode 0600. Newly created artifact directories use mode 0700; existing directories must be owned and not group/world writable. Manifests retain paths, UUIDs, selected dates/flags/statuses, dependency edges and fingerprints—not prompt text, titles or goal objectives.

The cooperating apply lock is `/tmp/codex-cleanup-UID/apply.lock`, owned and private to the current user. Lock files are not removed while coordinating runs. This does not lock out Codex itself; all foreign clients must stay closed.

Journal records are ordered `start`, then `intent → complete` pairs. “Complete” means both API acknowledgement and verification succeeded. Missing/partial/malformed records or a pending intent prohibit automatic continuation. The tool does not implement rollback or automatic ambiguous-request recovery.
