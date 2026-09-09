# ADR-001: Use a local child-process sidecar for speech recognition

## Status

Accepted

## Date

2026-09-10

## Context

Pi Voice needs to record microphone audio, keep a 600M-parameter speech model warm between utterances, and insert editable text into an existing Pi session. Pi must remain the sole agent and retain its context, tools, and permission gates.

The boundary handles untrusted process output and potentially sensitive transcripts. It must not expose a network service or let recognition errors submit tool instructions automatically.

## Decision

Use two cooperating components:

1. A Pi TypeScript extension owns keyboard handling, editor integration, visible recording state, transcript review, and sidecar lifecycle.
2. A persistent Python child process owns microphone capture and local Parakeet inference.

The processes communicate using validated, size-bounded newline-delimited JSON over stdin/stdout. The Python process keeps protocol output on the original stdout and redirects later library output to stderr so model logs cannot corrupt framing. The TypeScript client suppresses sidecar stderr because model libraries may include transcript content in diagnostics.

The sidecar loads the model lazily and serializes inference. Recording is capped at 60 seconds. Cancellation invalidates an active operation immediately; if a one-shot model call cannot stop internally, its result is discarded. A hard timeout terminates the child process.

Recognized text is inserted into Pi's editor only when the editor still matches the snapshot taken at recording start. The user must explicitly submit it.

## Alternatives considered

### Run speech recognition inside the TypeScript extension

Rejected because the selected Apple Silicon backend is Python/MLX-based. Embedding it would either require a different runtime or add a less mature binding.

### Start one Python process per utterance

Rejected because model load time would make every dictation turn slow and repeatedly allocate model memory.

### Expose a local HTTP or WebSocket service

Rejected because a network listener expands the attack surface and adds authentication, port management, and lifecycle complexity without benefit for a single parent process.

### Send transcripts directly to Pi

Rejected because speech recognition errors could trigger unintended code or computer actions. Editor insertion preserves a review step.

### Add a second LLM or computer-control loop

Rejected because it would duplicate Pi's context and authority, complicate permissions, and make behavior less predictable.

## Consequences

- Audio processing remains local apart from initial model download.
- The model stays warm for low-latency subsequent turns.
- The child-process protocol and lifecycle require explicit validation and cleanup.
- Cancellation cannot always stop in-flight model compute, but stale results never reach the editor.
- Pi Voice is currently tied to Apple Silicon macOS and the MLX Parakeet backend.
- Users need `uv` so a Git-installed Pi package can create and maintain its isolated Python environment.
