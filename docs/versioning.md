# Versioning

Three things carry a version, and they are deliberately not the same number.

| What | Form | Meaning | Today |
|---|---|---|---|
| Architecture milestone | git tag `core-vX.Y` (later `api-vX.Y`, …) | "this architectural section is complete and verified"; not a release, not installable by name | `core-v0.1`, `core-v0.2`, `api-v0.1` (private archive), `onboarding-v0.1` (public) |
| Package version | `pyproject.toml` / `linkplane --version`, SemVer | what a user installs; describes the shipped feature set | `0.4.0` |
| Public release | git tag `vX.Y.Z` on the exact commit `pyproject.toml` declares | the only thing that gets a changelog entry, a package, and support | none yet |
| Local API major | URL prefix `/vN/` (+ `linkplane.api/N` in `/vN/health` and the `Linkplane-Api` header) | interface compatibility of the local HTTP API: additive changes stay in `/vN`, breaking ones open `/vN+1` (`docs/local-api-design.md` §20) | `/v1` served by `linkplaned` (section closed by `api-v0.1`; not a release) |

## Package version policy

- `0.MINOR.PATCH` until a public 1.0. `MINOR` increments once per shipped section
  (0.1 Core v0.1, 0.2 Core v0.2, 0.3 automation + refinement, 0.4 API v0.1); `PATCH` for fixes between
  sections. The pre-refinement `0.9.0` was a placeholder from the first prototype and was
  never published, so resetting below it broke no one (`docs/refinement-pass-plan.md` §9).
- The package version changes in the same commit that finishes the section, and only
  there. Milestone tags and package versions are set in separate commits so history shows
  which is which.
- `CONTRACT_VERSION` (typed in-process surface) and `schema_version` (the `--json`
  envelope) are independent of all three and follow `docs/api-contracts.md`.
- A public release is cut by tagging `vX.Y.Z` on a commit whose `pyproject.toml` says
  `X.Y.Z`; never move a `vX.Y.Z` tag; never create one below an existing public one.
