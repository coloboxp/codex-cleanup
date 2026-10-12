# codex-cleanup
A dependency-free Python library and CLI for reviewing and removing old **archived** Codex conversations—not projects or workspace files.

**Alpha (`0.1.0a1`): deletion is irreversible. Back up your Codex and SQLite homes first. This is not an official OpenAI tool.**

## Quick start
Download this repository using **Code → Download ZIP**, or clone it. Open the extracted/cloned directory. Nothing to install; no package registry is required.

Requires Python **3.9+**, macOS or Linux, and an existing supported Codex store.

```sh
python3 -m codex_cleanup --help
python3 -m codex_cleanup scan --days 30
```

`scan` reads the stores without issuing deletion requests. It prints candidate counts, logical rollout sizes, preservation reasons, and the location of a **private local manifest**. It does not start an app-server.

For a predictable new manifest filename:

```sh
python3 -m codex_cleanup scan --days 30 --out "$HOME/.codex-cleanup/review.json"
python3 -m json.tool "$HOME/.codex-cleanup/review.json"
```

Manifests are never overwritten. Choose another filename if it already exists. Keep manifests outside your Codex/SQLite homes and outside this repository; they contain private paths and thread IDs.

## Apply an explicitly reviewed plan
1. Make a backup while Codex is closed.
2. Close the Codex desktop app, CLI sessions, IDE clients, and daemon. Keep them closed for the entire cleanup.
3. Run a fresh scan and review the manifest’s `groups` and `holds`.
4. Start with **one request**. A single request can delete an entire approved spawned subtree.

```sh
python3 -m codex_cleanup apply "$HOME/.codex-cleanup/review.json" \
  --confirm-delete --max-requests 1
```

Successful bounded batches report `remaining_requests`. Continue with the same manifest only after a successful batch; the journal checks prior completions. The default batch limit is 100 requests.

When `remaining_requests` is zero:

```sh
python3 -m codex_cleanup verify "$HOME/.codex-cleanup/review.json"
```

`verify` checks **all** planned deletions; it intentionally fails on an unfinished batch. Apply itself verifies its completed subset after each request.

**On interruption, timeout, protocol error, or preservation failure: stop.** An unresolved intent is never automatically retried. Preserve the journal, inspect the backup and current stores, and perform read-only verification. A fresh scan is not proof that an ambiguous request is safe to retry.

## Options and diagnostics
Each command has `--help`.

- `scan --days N`: rolling last-activity cutoff; default 30 days.
- `scan --before 2025-01-01T00:00:00Z`: exclusive, timezone-aware cutoff instead of `--days`; future cutoffs are refused.
- `scan --keep-thread UUID`: repeatable explicit protection.
- `scan --keep-cwd /workspace/keep`: repeatable protection for that resolved workspace subtree.
- `scan` / `doctor --home PATH --sqlite-home PATH`: custom homes. Defaults use `CODEX_HOME`, `CODEX_SQLITE_HOME`, then `~/.codex`.
- `scan` / `doctor --state-dir PATH`: private artifact directory; default `~/.codex-cleanup`.
- `apply` / `doctor --codex-bin PATH`: an explicitly trusted local executable. No runtime is downloaded.
- `doctor`: runtime/version, process, ownership and platform diagnostics; never starts an app-server. Diagnostic JSON may report blockers even when the command exits successfully.

The optional launcher works as `python3 codex-cleanup ...` or `./codex-cleanup ...`.

## Safety policy
Eligibility requires an old archived metadata record and exactly one owned archived rollout. Last activity uses the latest supported metadata/history/header timestamp and rollout modification time.

The tool preserves unarchived, recent, pinned, background-enabled, queued, nonterminal-goal, history-active, explicitly kept, and ambiguous threads. It transitively preserves bases needed by surviving forks/spawned children. Project membership alone is not a preservation rule. Cycles and duplicate logical rollout files are held.

Deletion uses the supported private-stdio **`thread/delete` API**, never SQL deletion, manual rollout deletion, `VACUUM`, or database wiping in production code. Compression/migration workers are disabled for the owned server. Its open file descriptors must prove the intended SQLite home. No thread is started/resumed and no model turn is requested.

Snapshots are rechecked before requests. The journal durably records intent before sending and completion only after verification. A per-user cooperating cleanup lock is independent of manifest/state/home choices.

## Compatibility and limitations
- Apply accepts only reviewed Codex **0.162.0 / 0.162.1**. There is no version bypass.
- Supported store versions: `state_5.sqlite`, `thread_history_1.sqlite`, `goals_1.sqlite`, `queue_1.sqlite`. Newer versions, unknown safety/schema/source relationships, missing metadata rollouts, compressed or symlinked rollouts, and noncanonical/outside-home metadata paths are refused.
- Stores must be owned by the current user and not shared-writable. Shared SQLite homes spanning different Codex homes are unsupported.
- macOS requires `lsof`; Linux requires inspectable `/proc` file descriptors. Restricted process inspection blocks apply.
- The lock coordinates this tool, **not other applications**. Repeated writer checks cannot make the final check-to-delete interval atomic. Do not reopen Codex or another writer during apply.
- Apply starts a trusted local Codex runtime, which can perform normal startup initialization or configured plugin/network activity. It is not a side-effect-free sandbox. Trust your executable and configuration.
- Verification covers rollout stat/header fingerprints, selected thread/history/goal/queue metadata, index-record hashes, unchanged desktop-state hash, deletion absence and SQLite integrity. It is **not** byte-for-byte verification of every database field or transcript body and cannot undo deletion.
- Conservative checks may refuse legitimate migrations or desktop-state changes. Review rather than bypass them. Full scans/integrity checks can be slow on large stores.
- Reported sizes are logical rollout bytes, not guaranteed disk recovery; APFS snapshots, shared extents and SQLite free pages can retain space.
- Development validation uses synthetic stores and a fake server only. It does not certify every real installation or runtime configuration.

## Library API
Import from the downloaded source directory, or put that directory on your own Python import path:

```python
from codex_cleanup import Settings, scan, write_manifest

settings = Settings.default(days=30)
plan = scan(settings)
print(plan.summary())
write_manifest(plan, settings.state_dir / "review.json")
```

Exports: `Settings`, `CleanupPlan`, `CleanupError`, `scan`, `write_manifest`, `load_manifest`, `doctor`, `apply`, and `verify`. See [API reference](docs/api.md) for signatures.

## Contributions and license
PRs and first-time contributors are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md), and [SECURITY.md](SECURITY.md).

MIT licensed; see [LICENSE](LICENSE).
