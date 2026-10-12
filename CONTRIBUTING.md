# Contributing
PRs, questions, bug reports and first-time contributors are welcome. This is a small alpha project, not a production-ready cleanup service.

## Development setup
Approximately two minutes with Python 3.9+ already installed. From a downloaded/cloned checkout:

```sh
python3 -m codex_cleanup --help
python3 -m unittest discover -s tests -v
python3 -m compileall -q codex_cleanup tests
```

No runtime dependencies or package installation are required. Optional development linting uses Ruff 0.15.4:

```sh
ruff check .
ruff format --check .
```

CI uses synthetic fixtures only, on Linux and macOS. Tests must never discover/start a real Codex runtime, access a real Codex store, stop existing processes, or rely on private machine data. Fake writable stores require an explicit synthetic marker.

## Reporting bugs and proposing changes
Use the repository’s Issues tab. Include Python/OS/Codex versions, the command shape without private arguments, the error category, and a minimal **synthetic** reproduction.

Do not attach real manifests, journals, databases, rollouts, configuration, tokens, usernames, company/project names, titles, prompts or home-directory paths. Consult [SECURITY.md](SECURITY.md) for vulnerabilities.

Useful contribution areas include synthetic edge cases, documentation, performance measurements on generated stores, and carefully researched runtime/layout compatibility. Discuss significant safety or compatibility changes in an issue before implementation.

## Pull requests
1. Keep the change focused.
2. Add a synthetic regression test; compatibility changes need upstream schema/behavior evidence, not just a wider version allowlist.
3. Run tests, compilation and the optional lint/format checks.
4. Update documentation and the Unreleased changelog when behavior changes.
5. Explain what is preserved, what is removed, and which uncertainty still blocks deletion.

Do not add unsafe bypasses, direct SQL/filesystem cleanup, package publishing, or real-store CI.

This project is maintained on a best-effort basis with no guaranteed response time. A polite follow-up after a week is welcome.

## Conduct
Be respectful, constructive and privacy-conscious. Critique code and behavior, not people. Harassment, personal attacks and disclosure of private data are not acceptable.
