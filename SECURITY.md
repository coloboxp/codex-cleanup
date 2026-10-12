# Security and data-loss reports
Only the latest alpha is maintained, and no production safety guarantee is offered. Keep backups and all foreign clients closed during apply.

Use the repository’s **Security → Report a vulnerability** feature for a privately disclosed vulnerability or data-loss bug. If private reporting is unavailable, open an issue requesting a private reporting channel **without** exploit details or private data.

Provide a synthetic reproduction, affected versions and expected preservation boundary. Never upload real Codex stores, manifests, journals, rollouts, credentials or identifying paths.

Treat your Codex binary/configuration and local manifest as trusted input. Checksums detect corruption, not malicious replacement. The cooperating lock and writer checks are not an operating-system sandbox or atomic exclusion of arbitrary applications.

Stop after any uncertain deletion outcome. Preserve local evidence privately; do not delete the journal or repeatedly resend a request. There is no automated rollback or guaranteed response timeline.
