# Release Process

This document outlines the standard process for releasing a new version of Sutra CLI.

## Release Steps

1.  **Prepare the version bump:**
    *   Update the `VERSION` string in `sutra_cli/main.py`.
    *   Ensure all tests pass: `pytest tests/test_cli_smoke.py`.
    *   Update `docs/progress.md` or `CHANGELOG.md` if applicable.

2.  **Commit and Push:**
    *   Commit the changes: `git commit -m "chore: bump version to X.Y.Z"`.
    *   Push to `main`: `git push origin main`.

3.  **Create and Push Tag:**
    *   The PyPI and GitHub release workflow is triggered by a version tag.
    *   Create a tag: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`.
    *   Push the tag: `git push origin vX.Y.Z`.

4.  **Verify Automation:**
    *   Monitor the [GitHub Actions](https://github.com/sairintechnologycom/sutra/actions) tab.
    *   The `pypi-release` job will publish to PyPI.
    *   The `create-release` job will create a GitHub Release with standalone binaries.

## Automation Guardrails

The GitHub workflow is configured to:
- Only publish when a tag matching `v*` is pushed.
- Fail if the version in `sutra_cli/main.py` does not match the git tag (added in v0.3.8+).
