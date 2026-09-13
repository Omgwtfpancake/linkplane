# Release process

How a Linkplane package version becomes a public release. Versions, milestone tags and
public tags are defined in `docs/versioning.md`.

## Sequence

```text
release-readiness pass approved by the founder
  → commit release docs and metadata (no version change)
  → separate commit: bump the package version (pyproject.toml, __version__, the health
    test) — "Bump package version to X.Y.Z"
  → the release gate below, on that commit
  → annotated tag vX.Y.Z on that exact commit
  → build the wheel and sdist from the tagged commit
  → GitHub pre-release (alpha/beta) or release, with both artifacts and the notes from
    docs/releases/vX.Y.Z.md
```

## Release gate (must all pass on the commit to be tagged)

1. `make test` behind the external-tool shims (adb, pacman, apt-get, dnf, sudo, systemctl)
   with the ADB server stopped: all pass, zero external invocations.
2. Wheel and sdist build cleanly from a clean checkout (`python -m build`).
3. Artifact audit: only the `linkplane` package, `LICENSE`, `README.md`, `pyproject.toml`
   (and `tests/` in the sdist only if intended); no docs, Git metadata, state, private
   paths, or identifiers.
4. Temporary pipx install of the wheel: `linkplane --version` prints the version,
   `--help`, `setup --help`, `uninstall --dry-run --json` work. No `PYTHONPATH`.
5. Every command in `README.md`, `docs/install.md`, `docs/troubleshooting.md` and the
   release notes exists in the CLI help.
6. `git status` clean; local `main` equals `origin/main`.
7. Real-device smoke on the paired phone from the built wheel: `setup` rerun, `status`,
   `battery`, `daemon status`, and a disconnect/reconnect seen by `events`. The full
   clean-machine qualification (`docs/clean-machine-onboarding.md`) is repeated only when
   onboarding code changed since the last qualified commit.

## Artifacts

`linkplane-X.Y.Z-py3-none-any.whl` (the install target) and `linkplane-X.Y.Z.tar.gz`
(source), both attached to the GitHub release, with their SHA-256 sums in the release text.

## Install and upgrade for tag-based releases

```sh
pipx install https://github.com/Omgwtfpancake/linkplane/releases/download/vX.Y.Z/linkplane-X.Y.Z-py3-none-any.whl
pipx install --force https://github.com/Omgwtfpancake/linkplane/releases/download/vNEW/linkplane-NEW-py3-none-any.whl   # upgrade
linkplane setup                                                                                                        # restarts a stale daemon
```

`pipx upgrade linkplane` only works once installs come from PyPI; until then upgrade is a
`--force` reinstall of the newer wheel, after which `linkplane setup` verifies and restarts
the daemon.
