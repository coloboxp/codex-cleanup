# Changelog
Notable changes are recorded here. Alpha behavior and formats may change without backward compatibility.

## Unreleased
No changes yet.

## 0.1.0a2 — 2026-10-12
### Fixed
- Refuse malformed non-null fork ancestry, including empty or false values, instead of treating it as absent.
- Use the package version consistently in the stdio handshake.
- Add synthetic regressions for all affected value types.

## 0.1.0a1 — 2026-10-12
Initial source-only alpha.

### Added
- Standard-library Python library and download-and-run CLI: scan, apply, verify, doctor.
- Rolling age policy, explicit thread/workspace exclusions, survivor ancestry protection, cascade-aware ordering.
- Private corruption-checked manifests, durable per-request journal, bounded successful-batch continuation.
- Narrow runtime/schema gates, effective SQLite-home verification, ownership and writer checks.
- Synthetic policy, storage, manifest, protocol, interruption and preservation tests.
- MIT license, contribution/security guidance and pinned read-only CI.

### Known limitations
- Apply is restricted to Codex 0.162.0/0.162.1 and reviewed local layouts.
- Deletion has no undo. Foreign clients must remain closed; checks do not provide an atomic writer exclusion.
- Verification covers supported metadata and fingerprints, not every database field or transcript byte.
- No package registry publication, runtime download, automatic process shutdown, rollback or ambiguous-request retry.
