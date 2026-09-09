# Modified from Hugging Face speech-to-speech; see NOTICE.
"""Concurrent state machine for the Pi voice sidecar."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread
from typing import Any, Literal, Protocol, TypeAlias

from pi_voice.protocol import (
    ProtocolError,
    VoiceRequest,
    error_response,
    status_notification,
    success_response,
)

VoiceState: TypeAlias = Literal["idle", "recording", "transcribing", "stopped"]
MessageEmitter: TypeAlias = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language_code: str | None = None


class VoiceRuntime(Protocol):
    def start_recording(self) -> None: ...

    def stop_recording(self) -> object: ...

    def transcribe(self, audio: object) -> TranscriptionResult: ...

    def cancel(self) -> None: ...

    def shutdown(self) -> None: ...


class VoiceService:
    """Dispatch validated requests while keeping cancellation responsive."""

    def __init__(self, runtime: VoiceRuntime, emit: MessageEmitter) -> None:
        self._runtime = runtime
        self._emit = emit
        self._lock = Lock()
        self._state: VoiceState = "idle"
        self._active_token = 0
        self._active_request_id: str | None = None

    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state

    def handle(self, request: VoiceRequest) -> None:
        try:
            if request.method == "ping":
                self._emit(success_response(request.request_id, {"state": self.state}))
            elif request.method == "record.start":
                self._record_start(request)
            elif request.method == "record.stop":
                self._record_stop(request)
            elif request.method == "cancel":
                self._cancel(request)
            elif request.method == "shutdown":
                self._shutdown(request)
        except ProtocolError as exc:
            self._emit(error_response(request.request_id, exc))
        except Exception as exc:
            self._emit(error_response(request.request_id, self._runtime_error(exc)))

    def _record_start(self, request: VoiceRequest) -> None:
        self._require_state("idle")
        self._runtime.start_recording()
        self._set_state("recording")
        self._emit(success_response(request.request_id, {"state": "recording"}))

    def _record_stop(self, request: VoiceRequest) -> None:
        self._require_state("recording")
        try:
            audio = self._runtime.stop_recording()
        except Exception:
            self._set_state("idle")
            raise
        self._start_operation(request.request_id, lambda: self._transcribe(audio))

    def _transcribe(self, audio: object) -> dict[str, Any]:
        result = self._runtime.transcribe(audio)
        transcript = result.text.strip()
        if not transcript:
            raise ProtocolError("NO_SPEECH", "No speech was detected")
        return {
            "state": "idle",
            "transcript": transcript,
            "languageCode": result.language_code,
        }

    def _start_operation(
        self,
        request_id: str,
        operation: Callable[[], dict[str, Any]],
    ) -> None:
        with self._lock:
            if self._state == "stopped":
                raise ProtocolError("INVALID_STATE", "Voice sidecar is stopped")
            self._active_token += 1
            token = self._active_token
            self._active_request_id = request_id
            self._state = "transcribing"
        self._emit(status_notification("transcribing"))

        worker = Thread(
            target=self._run_operation,
            args=(token, request_id, operation),
            name="pi-voice-transcribing",
            daemon=True,
        )
        worker.start()

    def _run_operation(
        self,
        token: int,
        request_id: str,
        operation: Callable[[], dict[str, Any]],
    ) -> None:
        response: dict[str, Any]
        try:
            result = operation()
            response = success_response(request_id, result)
        except ProtocolError as exc:
            response = error_response(request_id, exc)
        except Exception as exc:
            response = error_response(request_id, self._runtime_error(exc))

        with self._lock:
            if token != self._active_token or request_id != self._active_request_id:
                return
            self._active_request_id = None
            self._state = "idle"
        self._emit(status_notification("idle"))
        self._emit(response)

    def _cancel(self, request: VoiceRequest) -> None:
        with self._lock:
            if self._state == "stopped":
                raise ProtocolError("INVALID_STATE", "Voice sidecar is stopped")
            active_request_id = self._active_request_id
            self._active_token += 1
            self._active_request_id = None
            self._state = "idle"

        cleanup_error: Exception | None = None
        try:
            self._runtime.cancel()
        except Exception as exc:
            cleanup_error = exc
        finally:
            if active_request_id is not None:
                self._emit(error_response(active_request_id, ProtocolError("CANCELLED", "Operation cancelled")))
            self._emit(status_notification("idle"))
        if cleanup_error is not None:
            raise cleanup_error
        self._emit(success_response(request.request_id, {"state": "idle"}))

    def _shutdown(self, request: VoiceRequest) -> None:
        with self._lock:
            active_request_id = self._active_request_id
            self._active_token += 1
            self._active_request_id = None
            self._state = "stopped"

        cleanup_error: Exception | None = None
        try:
            self._runtime.shutdown()
        except Exception as exc:
            cleanup_error = exc
        finally:
            if active_request_id is not None:
                self._emit(error_response(active_request_id, ProtocolError("CANCELLED", "Sidecar shutting down")))
            self._emit(status_notification("stopped"))
        if cleanup_error is not None:
            raise cleanup_error
        self._emit(success_response(request.request_id, {"state": "stopped"}))

    def _require_state(self, expected: VoiceState) -> None:
        actual = self.state
        if actual != expected:
            raise ProtocolError("INVALID_STATE", f"Expected state {expected}, current state is {actual}")

    def _set_state(self, state: VoiceState) -> None:
        with self._lock:
            self._state = state
        self._emit(status_notification(state))

    @staticmethod
    def _runtime_error(exc: Exception) -> ProtocolError:
        message = str(exc).strip() or exc.__class__.__name__
        return ProtocolError("RUNTIME_ERROR", message)
