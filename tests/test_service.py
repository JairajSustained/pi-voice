from __future__ import annotations

from threading import Event, Lock
from typing import Any

from pi_voice.protocol import VoiceRequest
from pi_voice.service import TranscriptionResult, VoiceService


class FakeRuntime:
    def __init__(self) -> None:
        self.audio = object()
        self.cancelled = Event()
        self.transcribe_started = Event()
        self.release_transcription = Event()
        self.block_transcription = False
        self.fail_transcription = False
        self.fail_cancel = False
        self.shutdown_called = False

    def start_recording(self) -> None:
        pass

    def stop_recording(self) -> object:
        return self.audio

    def transcribe(self, audio: object) -> TranscriptionResult:
        assert audio is self.audio
        self.transcribe_started.set()
        if self.block_transcription:
            self.release_transcription.wait(timeout=2)
        if self.fail_transcription:
            raise RuntimeError("model exploded with internal details")
        return TranscriptionResult(text="Open Safari", language_code="en")

    def cancel(self) -> None:
        self.cancelled.set()
        self.release_transcription.set()
        if self.fail_cancel:
            raise RuntimeError("audio cleanup failed")

    def shutdown(self) -> None:
        self.shutdown_called = True
        self.cancel()


class MessageCollector:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.changed = Event()
        self.lock = Lock()

    def emit(self, message: dict[str, Any]) -> None:
        with self.lock:
            self.messages.append(message)
            self.changed.set()

    def wait_for_id(self, request_id: str) -> dict[str, Any]:
        for _ in range(20):
            with self.lock:
                match = next((message for message in self.messages if message.get("id") == request_id), None)
            if match is not None:
                return match
            self.changed.wait(timeout=0.1)
            self.changed.clear()
        raise AssertionError(f"No response for {request_id}: {self.messages}")


def request(request_id: str, method: str, params: dict[str, Any] | None = None) -> VoiceRequest:
    return VoiceRequest(request_id=request_id, method=method, params=params or {})  # type: ignore[arg-type]


def test_recording_transcribes_and_returns_to_idle() -> None:
    runtime = FakeRuntime()
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "record.start"))
    service.handle(request("2", "record.stop"))

    assert collector.wait_for_id("1") == {"id": "1", "ok": True, "result": {"state": "recording"}}
    assert collector.wait_for_id("2") == {
        "id": "2",
        "ok": True,
        "result": {"state": "idle", "transcript": "Open Safari", "languageCode": "en"},
    }
    assert service.state == "idle"


def test_service_rejects_a_new_operation_while_transcribing() -> None:
    runtime = FakeRuntime()
    runtime.block_transcription = True
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "record.start"))
    service.handle(request("2", "record.stop"))
    assert runtime.transcribe_started.wait(timeout=1)
    service.handle(request("3", "record.start"))

    response = collector.wait_for_id("3")
    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_STATE"
    runtime.release_transcription.set()
    assert collector.wait_for_id("2")["ok"] is True


def test_cancel_terminates_the_active_request_exactly_once() -> None:
    runtime = FakeRuntime()
    runtime.block_transcription = True
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "record.start"))
    service.handle(request("2", "record.stop"))
    assert runtime.transcribe_started.wait(timeout=1)
    service.handle(request("3", "cancel"))

    active_response = collector.wait_for_id("2")
    cancel_response = collector.wait_for_id("3")
    assert active_response["error"]["code"] == "CANCELLED"
    assert cancel_response == {"id": "3", "ok": True, "result": {"state": "idle"}}
    assert runtime.cancelled.is_set()
    assert [message.get("id") for message in collector.messages].count("2") == 1


def test_cancel_terminalizes_active_work_even_when_cleanup_fails() -> None:
    runtime = FakeRuntime()
    runtime.block_transcription = True
    runtime.fail_cancel = True
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "record.start"))
    service.handle(request("2", "record.stop"))
    assert runtime.transcribe_started.wait(timeout=1)
    service.handle(request("3", "cancel"))

    assert collector.wait_for_id("2")["error"]["code"] == "CANCELLED"
    assert collector.wait_for_id("3")["error"]["code"] == "RUNTIME_ERROR"
    assert [message.get("id") for message in collector.messages].count("2") == 1


def test_runtime_failure_is_bounded_and_service_recovers() -> None:
    runtime = FakeRuntime()
    runtime.fail_transcription = True
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "record.start"))
    service.handle(request("2", "record.stop"))

    response = collector.wait_for_id("2")
    assert response["ok"] is False
    assert response["error"]["code"] == "RUNTIME_ERROR"
    assert "model exploded" in response["error"]["message"]
    assert service.state == "idle"


def test_shutdown_cancels_work_and_stops_the_runtime() -> None:
    runtime = FakeRuntime()
    collector = MessageCollector()
    service = VoiceService(runtime, collector.emit)

    service.handle(request("1", "shutdown"))

    assert collector.wait_for_id("1") == {"id": "1", "ok": True, "result": {"state": "stopped"}}
    assert runtime.shutdown_called is True
    assert service.state == "stopped"
