# Modified from Hugging Face speech-to-speech; see NOTICE.
"""Executable NDJSON sidecar used by the project-local Pi voice extension."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from threading import Lock
from typing import Any, TextIO

from pi_voice.protocol import MAX_LINE_BYTES, ProtocolError, error_response, parse_request
from pi_voice.runtime import LocalSpeechRuntime
from pi_voice.service import VoiceRuntime, VoiceService

logger = logging.getLogger(__name__)


def iter_bounded_lines(stdin: TextIO) -> Iterator[str | ProtocolError]:
    """Read newline-delimited input without buffering an unbounded line."""

    characters: list[str] = []
    byte_count = 0
    discarding = False
    while chunk := stdin.readline(4_096):
        for character in chunk:
            if character == "\n":
                if discarding:
                    yield ProtocolError("REQUEST_TOO_LARGE", f"Request exceeds {MAX_LINE_BYTES} bytes")
                elif characters:
                    yield "".join(characters)
                characters = []
                byte_count = 0
                discarding = False
                continue
            if discarding:
                continue
            byte_count += len(character.encode("utf-8"))
            if byte_count > MAX_LINE_BYTES:
                characters = []
                discarding = True
                continue
            characters.append(character)

    if discarding:
        yield ProtocolError("REQUEST_TOO_LARGE", f"Request exceeds {MAX_LINE_BYTES} bytes")
    elif characters:
        yield "".join(characters)


def run_sidecar(stdin: TextIO, stdout: TextIO, runtime: VoiceRuntime) -> None:
    """Serve sidecar requests until shutdown or input EOF."""

    output_lock = Lock()

    def emit(message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with output_lock:
            stdout.write(encoded + "\n")
            stdout.flush()

    service = VoiceService(runtime, emit)
    try:
        for line in iter_bounded_lines(stdin):
            if isinstance(line, ProtocolError):
                emit(error_response(None, line))
                continue
            if not line.strip():
                continue
            try:
                request = parse_request(line)
            except ProtocolError as exc:
                emit(error_response(exc.request_id, exc))
                continue
            service.handle(request)
            if request.method == "shutdown":
                break
    finally:
        runtime.shutdown()


def main() -> None:
    # Model libraries and Rich handlers occasionally print to stdout. Preserve
    # the original stream for protocol output and route every later print to
    # stderr so one stray log line cannot corrupt NDJSON framing.
    protocol_output = sys.stdout
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        run_sidecar(sys.stdin, protocol_output, LocalSpeechRuntime())
    except KeyboardInterrupt:
        logger.info("Pi voice sidecar interrupted")


if __name__ == "__main__":
    main()
