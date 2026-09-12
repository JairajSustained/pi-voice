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

## Dictation sequence

This sequence starts with voice enabled and Pi idle. The extension starts and checks the persistent sidecar when voice is enabled; model loading waits until transcription is needed.

```mermaid
sequenceDiagram
    actor User
    participant Ext as Pi TypeScript extension
    participant Editor as Pi editor
    participant Sidecar as Python sidecar / VoiceService
    participant Runtime as LocalSpeechRuntime
    participant Model as Parakeet via MLX Audio
    participant Pi as Pi coding agent

    User->>Ext: Hold Space past recording threshold
    Ext->>Editor: Read draft snapshot
    Editor-->>Ext: Current draft
    Ext->>Sidecar: record.start (NDJSON over stdin)
    Sidecar->>Runtime: Start microphone capture
    Sidecar-->>Ext: Recording status and acknowledgement
    Note over Runtime: Buffer 16 kHz mono PCM in memory; cap at 60 seconds
    User->>Ext: Release Space
    Ext->>Sidecar: record.stop
    Sidecar->>Runtime: Stop capture and collect audio
    Runtime-->>Sidecar: Audio waveform
    Sidecar-->>Ext: Transcribing status
    Note over Sidecar: Run transcription on a worker; keep request handling responsive
    Sidecar->>Runtime: Transcribe audio
    Runtime->>Model: Load lazily if needed; decode audio
    Note over Runtime,Model: Inference is serialized; loaded model is reused
    Model-->>Runtime: Recognized text
    Runtime-->>Sidecar: Transcription result
    alt Operation is still active
        Sidecar-->>Ext: Idle status and transcript response over stdout
        Ext->>Editor: Compare current draft with snapshot
        alt Draft is unchanged
            Ext->>Editor: Append transcript with spacing
            User->>Editor: Review and optionally edit
            User->>Pi: Press Enter to submit editor text
            Note over Pi: Normal model, tools and permission flow
        else Draft changed
            Ext-->>User: Notify; do not insert transcript
        end
    else Operation was cancelled or shut down
        Note over Sidecar: Invalidated operation token suppresses late result
    end
```

Cancellation invalidates the active operation and clears recording resources; an in-flight model call may finish internally, but its late result is discarded. A request timeout terminates the sidecar, allowing a later request to start a fresh process. Empty, too-short, and duration-limited recordings fail without inserting text. Protocol validation and the editor snapshot check protect the boundary before user submission.

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
