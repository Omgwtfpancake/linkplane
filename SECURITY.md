# Security policy

Linkplane controls a phone over ADB, stores API client tokens, serves a local HTTP API,
moves files, and runs consent-gated automation actions. Reports about any of that are
welcome and taken seriously.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting for this repository
(**Security → Report a vulnerability** on https://github.com/Omgwtfpancake/linkplane).
That path keeps the report between you and the maintainer until a fix is available.

If that form is unavailable, open an issue titled "Security: please contact me" with no
details, and the maintainer will reach you through GitHub.

Include what you can: the Linkplane version (`linkplane --version`), the OS, how to
reproduce, and the impact. Never include tokens, keys, or device serials.

## Scope and expectations

- Alpha software: fixes are best effort, but a report will be acknowledged and triaged.
- Supported versions: the latest release only.
- Design boundaries that are considered features, not bugs: the API listens on loopback
  only; tokens are stored as SHA-256 digests; `run` actions require explicit consent in the
  rules file; purge only ever removes Linkplane's own directories.
