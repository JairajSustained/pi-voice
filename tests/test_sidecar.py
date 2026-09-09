# Modified from Hugging Face speech-to-speech; see NOTICE.
from __future__ import annotations

import io
import json
from typing import Any

from pi_voice.protocol import MAX_LINE_BYTES
from pi_voice.service import TranscriptionResult
from pi_voice.sidecar import run_sidecar


class NoopRuntime:
    def __init__(self) -> None:
        self.shutdown_called = False

    def start_recording(self) -> None:
        pass

    def stop_recording(self) -> object:
        return object()

    def transcribe(self, _audio: object) -> TranscriptionResult:
        return TranscriptionResult("hello", "en")

    def cancel(self) -> None:
        pass

    def shutdown(self) -> None:
        self.shutdown_called = True


def parse_lines(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_sidecar_serves_requests_until_shutdown() -> None:
    stdin = io.BytesIO(
        '\n'.join(
            [
                '{"id":"1","method":"ping","params":{}}',
                '{"id":"2","method":"shutdown","params":{}}',
                '{"id":"3","method":"ping","params":{}}',
            ]
        ).encode()
    )
    stdout = io.StringIO()
    runtime = NoopRuntime()

    run_sidecar(stdin, stdout, runtime)

    messages = parse_lines(stdout)
    assert messages[0] == {"id": "1", "ok": True, "result": {"state": "idle"}}
    assert messages[-1] == {"id": "2", "ok": True, "result": {"state": "stopped"}}
    assert all(message.get("id") != "3" for message in messages)
    assert runtime.shutdown_called is True


def test_sidecar_returns_protocol_errors_without_exiting() -> None:
    stdin = io.BytesIO(
        '\n'.join(
            [
                "not-json",
                '{"id":"2","method":"ping","params":{}}',
                '{"id":"3","method":"shutdown","params":{}}',
            ]
        ).encode()
    )
    stdout = io.StringIO()

    run_sidecar(stdin, stdout, NoopRuntime())

    messages = parse_lines(stdout)
    assert messages[0]["error"]["code"] == "INVALID_JSON"
    assert any(message.get("id") == "2" and message["ok"] is True for message in messages)


def test_sidecar_correlates_validation_errors_after_a_valid_id() -> None:
    stdin = io.BytesIO(
        '\n'.join(
            [
                '{"id":"17","method":"unknown","params":{}}',
                '{"id":"18","method":"shutdown","params":{}}',
            ]
        ).encode()
    )
    stdout = io.StringIO()

    run_sidecar(stdin, stdout, NoopRuntime())

    assert parse_lines(stdout)[0] == {
        "id": "17",
        "ok": False,
        "error": {"code": "UNKNOWN_METHOD", "message": "Unknown method: unknown"},
    }


def test_sidecar_discards_an_oversized_line_and_continues() -> None:
    stdin = io.BytesIO(
        (
            "x" * (MAX_LINE_BYTES + 1)
            + '\n{"id":"2","method":"ping","params":{}}'
            + '\n{"id":"3","method":"shutdown","params":{}}\n'
        ).encode()
    )
    stdout = io.StringIO()

    run_sidecar(stdin, stdout, NoopRuntime())

    messages = parse_lines(stdout)
    assert messages[0]["error"]["code"] == "REQUEST_TOO_LARGE"
    assert any(message.get("id") == "2" and message["ok"] is True for message in messages)


def test_sidecar_rejects_invalid_utf8_and_continues() -> None:
    stdin = io.BytesIO(
        b'\xff\n{"id":"2","method":"ping","params":{}}\n'
        b'{"id":"3","method":"shutdown","params":{}}\n'
    )
    stdout = io.StringIO()

    run_sidecar(stdin, stdout, NoopRuntime())

    messages = parse_lines(stdout)
    assert messages[0]["error"]["code"] == "INVALID_JSON"
    assert any(message.get("id") == "2" and message["ok"] is True for message in messages)
