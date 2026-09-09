# Releasing Pi Voice

Pi Voice versions are immutable Git tags installed directly by Pi.

## Checklist

1. Confirm the release version is unused.
2. Update `version` in both `package.json` and `pyproject.toml`.
3. Update the version-pinned install command in `README.md`.
4. Run:

   ```bash
   uv lock --check
   uv run pytest
   uv run ruff check src tests
   uv run mypy
   npm ci
   npm run typecheck
   npm test
   ```

5. Open and merge a focused release pull request.
6. Update local `main` from GitHub.
7. Create an annotated `vX.Y.Z` tag pointing to the reviewed release commit.
8. Push the tag and create GitHub release notes.
9. Verify installation in a clean environment:

   ```bash
   pi install git:github.com/JairajSustained/pi-voice@vX.Y.Z
   ```

10. Run `/reload`, `/voice ping`, and one real recording/transcription test on Apple Silicon macOS.

Do not move or replace published version tags. Publish a new patch version for corrections.
