from __future__ import annotations

import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest

from pi_voice.runtime import DEFAULT_MODEL_REVISION, AudioRecorder, LocalSpeechRuntime, ParakeetTranscriber
from pi_voice.service import TranscriptionResult


class FakeRawInputStream:
    def __init__(self, **kwargs: Any) -> None:
        self.callback = kwargs["callback"]
        self.started = False
        self.stopped = False
        self.closed = False
        self.aborted = False
        self.closed_event = Event()

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()

    def feed(self, samples: np.ndarray) -> None:
        self.callback(samples.astype(np.int16).tobytes(), len(samples), None, None)


class FakeSoundDevice:
    def __init__(self) -> None:
        self.input_streams: list[FakeRawInputStream] = []

    def RawInputStream(self, **kwargs: Any) -> FakeRawInputStream:
        stream = FakeRawInputStream(**kwargs)
        self.input_streams.append(stream)
        return stream


class FakeTranscriber:
    def __init__(self) -> None:
        self.inputs: list[np.ndarray] = []
        self.cleaned = False

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        self.inputs.append(audio)
        return TranscriptionResult(text="Open Safari", language_code="en")

    def cleanup(self) -> None:
        self.cleaned = True


def test_audio_recorder_returns_normalized_mono_audio() -> None:
    sounddevice = FakeSoundDevice()
    recorder = AudioRecorder(sounddevice=sounddevice, minimum_seconds=0)

    recorder.start()
    sounddevice.input_streams[0].feed(np.array([-32768, 0, 16384, 32767], dtype=np.int16))
    audio = recorder.stop()

    assert audio.dtype == np.float32
    np.testing.assert_allclose(audio, [-1.0, 0.0, 0.5, 32767 / 32768])
    assert sounddevice.input_streams[0].stopped is True
    assert sounddevice.input_streams[0].closed is True


def test_audio_recorder_closes_a_stream_when_start_fails() -> None:
    class FailingInputStream(FakeRawInputStream):
        def start(self) -> None:
            raise RuntimeError("device start failed")

    stream = FailingInputStream(callback=lambda *_args: None)
    sounddevice = SimpleNamespace(RawInputStream=lambda **_kwargs: stream)
    recorder = AudioRecorder(sounddevice=sounddevice, minimum_seconds=0)

    with pytest.raises(RuntimeError, match="device start failed"):
        recorder.start()

    assert stream.aborted is True
    assert stream.closed is True


def test_audio_recorder_stops_the_microphone_at_the_duration_limit() -> None:
    sounddevice = FakeSoundDevice()
    recorder = AudioRecorder(sounddevice=sounddevice, minimum_seconds=0, maximum_seconds=0.02)

    recorder.start()
    stream = sounddevice.input_streams[0]
    assert stream.closed_event.wait(timeout=1)

    with pytest.raises(RuntimeError, match="maximum duration"):
        recorder.stop()

    assert stream.aborted is True


def test_audio_recorder_rejects_empty_recordings() -> None:
    recorder = AudioRecorder(sounddevice=FakeSoundDevice(), minimum_seconds=0.1)

    recorder.start()

    with pytest.raises(RuntimeError, match="too short"):
        recorder.stop()


def test_parakeet_transcriber_pins_the_model_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    expected_model = object()
    generate = ModuleType("mlx_audio.stt.generate")

    def load_model(model_name: str, **kwargs: object) -> object:
        captured.update(model_name=model_name, **kwargs)
        return expected_model

    generate.load_model = load_model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_audio.stt.generate", generate)

    transcriber = ParakeetTranscriber()

    assert transcriber._get_model() is expected_model
    assert captured["revision"] == DEFAULT_MODEL_REVISION


def test_local_runtime_reuses_the_transcriber() -> None:
    transcriber = FakeTranscriber()
    recorder = SimpleNamespace(
        start=lambda: None,
        stop=lambda: np.zeros(1600, dtype=np.float32),
        cancel=lambda: None,
    )
    runtime = LocalSpeechRuntime(recorder=recorder, transcriber_factory=lambda: transcriber)

    first = runtime.transcribe(np.zeros(1600, dtype=np.float32))
    second = runtime.transcribe(np.ones(1600, dtype=np.float32))

    assert first == TranscriptionResult(text="Open Safari", language_code="en")
    assert second.text == "Open Safari"
    assert len(transcriber.inputs) == 2
    assert all(item.dtype == np.float32 and item.ndim == 1 for item in transcriber.inputs)


def test_local_runtime_rejects_empty_audio() -> None:
    runtime = LocalSpeechRuntime(
        recorder=SimpleNamespace(start=lambda: None, stop=lambda: object(), cancel=lambda: None),
        transcriber_factory=FakeTranscriber,
    )

    with pytest.raises(RuntimeError, match="mono audio"):
        runtime.transcribe(np.array([], dtype=np.float32))


def test_runtime_cancel_discards_the_recording() -> None:
    cancelled = Event()
    runtime = LocalSpeechRuntime(
        recorder=SimpleNamespace(start=lambda: None, stop=lambda: object(), cancel=cancelled.set),
        transcriber_factory=FakeTranscriber,
    )

    runtime.cancel()

    assert cancelled.is_set()


def test_runtime_shutdown_cleans_the_loaded_transcriber() -> None:
    transcriber = FakeTranscriber()
    runtime = LocalSpeechRuntime(
        recorder=SimpleNamespace(start=lambda: None, stop=lambda: object(), cancel=lambda: None),
        transcriber_factory=lambda: transcriber,
    )
    runtime._transcriber = transcriber

    runtime.shutdown()

    assert transcriber.cleaned is True
