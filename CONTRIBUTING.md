# Contributing

Thanks for helping improve Pi Voice.

## Development setup

Pi Voice currently targets Apple Silicon macOS and Python 3.11+.

```bash
uv sync --python 3.11
uv run pytest
uv run ruff check src tests
uv run mypy
```

Load the extension from a checkout:

```bash
pi --no-extensions --extension ./extensions/pi-voice.ts
```

## Pull requests

- Keep changes focused and include tests for behavior changes.
- Preserve the transcript review step; do not auto-submit recognized speech.
- Do not add network listeners, response TTS, or a second autonomous agent loop without first proposing a new ADR.
- Keep protocol input and output validated and size-bounded.
- Never log or persist audio or transcript content by default.
- Do not commit model files, virtual environments, caches, or generated build artifacts.

By contributing, you agree that your contribution is licensed under Apache-2.0.
