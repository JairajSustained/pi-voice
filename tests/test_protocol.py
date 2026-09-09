# Modified from Hugging Face speech-to-speech; see NOTICE.
from __future__ import annotations

import json

import pytest

from pi_voice.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    VoiceRequest,
    error_response,
    parse_request,
    success_response,
)


def test_parse_request_accepts_a_known_method() -> None:
    request = parse_request('{"id":"request-1","method":"record.start","params":{}}')

    assert request == VoiceRequest(request_id="request-1", method="record.start", params={})


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("not-json", "INVALID_JSON"),
        ("[]", "INVALID_REQUEST"),
        ('{"method":"ping","params":{}}', "INVALID_REQUEST"),
        ('{"id":"1","method":"missing","params":{}}', "UNKNOWN_METHOD"),
        ('{"id":"1","method":"ping","params":[]}', "INVALID_PARAMS"),
    ],
)
def test_parse_request_rejects_invalid_input(line: str, code: str) -> None:
    with pytest.raises(ProtocolError) as raised:
        parse_request(line)

    assert raised.value.code == code


def test_parse_request_rejects_an_oversized_line() -> None:
    line = json.dumps({"id": "1", "method": "ping", "params": {"padding": "x" * MAX_LINE_BYTES}})

    with pytest.raises(ProtocolError) as raised:
        parse_request(line)

    assert raised.value.code == "REQUEST_TOO_LARGE"


def test_parse_request_preserves_a_valid_id_on_later_validation_errors() -> None:
    with pytest.raises(ProtocolError) as raised:
        parse_request('{"id":"request-17","method":"missing","params":{}}')

    assert raised.value.request_id == "request-17"


def test_parse_request_rejects_speak_for_the_stt_only_sidecar() -> None:
    with pytest.raises(ProtocolError, match="Unknown method: speak"):
        parse_request('{"id":"1","method":"speak","params":{"text":"hello"}}')


def test_response_helpers_use_one_stable_shape() -> None:
    assert success_response("1", {"state": "idle"}) == {
        "id": "1",
        "ok": True,
        "result": {"state": "idle"},
    }
    assert error_response("1", ProtocolError("INVALID_STATE", "Already recording")) == {
        "id": "1",
        "ok": False,
        "error": {"code": "INVALID_STATE", "message": "Already recording"},
    }
