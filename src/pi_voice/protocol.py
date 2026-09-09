# Modified from Hugging Face speech-to-speech; see NOTICE.
"""Validated newline-delimited JSON contract for the local Pi voice sidecar."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, cast

MAX_LINE_BYTES = 64 * 1024
MAX_REQUEST_ID_CHARS = 128
MAX_ERROR_MESSAGE_CHARS = 512

VoiceMethod: TypeAlias = Literal[
    "ping",
    "record.start",
    "record.stop",
    "cancel",
    "shutdown",
]
KNOWN_METHODS = frozenset({"ping", "record.start", "record.stop", "cancel", "shutdown"})


class ProtocolError(ValueError):
    """A request error safe to serialize across the sidecar boundary."""

    def __init__(self, code: str, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:MAX_ERROR_MESSAGE_CHARS]
        self.request_id = request_id


@dataclass(frozen=True)
class VoiceRequest:
    request_id: str
    method: VoiceMethod
    params: dict[str, Any]


def parse_request(line: str) -> VoiceRequest:
    """Parse and validate one sidecar request line."""

    try:
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ProtocolError("REQUEST_TOO_LARGE", f"Request exceeds {MAX_LINE_BYTES} bytes")
        payload = json.loads(line)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise ProtocolError("INVALID_JSON", "Request must be valid JSON") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("INVALID_REQUEST", "Request must be a JSON object")

    request_id = payload.get("id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > MAX_REQUEST_ID_CHARS:
        raise ProtocolError("INVALID_REQUEST", "Request id must be a non-empty bounded string")

    method = payload.get("method")
    if not isinstance(method, str):
        raise ProtocolError("INVALID_REQUEST", "Request method must be a string", request_id)
    if method not in KNOWN_METHODS:
        raise ProtocolError("UNKNOWN_METHOD", f"Unknown method: {method[:80]}", request_id)

    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError("INVALID_PARAMS", "Request params must be a JSON object", request_id)

    if params:
        raise ProtocolError("INVALID_PARAMS", f"Method {method} does not accept parameters", request_id)

    return VoiceRequest(request_id=request_id, method=cast(VoiceMethod, method), params=params)


def success_response(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def error_response(request_id: str | None, error: ProtocolError) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error": {"code": error.code, "message": error.message},
    }
    if request_id is not None:
        response["id"] = request_id
    return response


def status_notification(state: str) -> dict[str, Any]:
    return {"event": "status", "data": {"state": state}}
