# Modified from Hugging Face speech-to-speech; see NOTICE.
"""Local microphone and Parakeet speech-recognition adapters."""

from __future__ import annotations

import logging
import platform
import sys
from collections.abc import Callable
from threading import Lock, Timer
from typing import Any, Protocol

import numpy as np

from pi_voice.service import TranscriptionResult

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"


class Recorder(Protocol):
    def start(self) -> None: ...

    def stop(self) -> object: ...

    def cancel(self) -> None: ...


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> TranscriptionResult: ...

    def cleanup(self) -> None: ...


class AudioRecorder:
    """Bounded tap-to-start/tap-to-stop mono PCM recorder."""

    def __init__(
        self,
        *,
        sounddevice: Any | None = None,
        input_device: int | None = None,
        sample_rate: int = SAMPLE_RATE,
        block_size: int = 1_024,
        minimum_seconds: float = 0.15,
        maximum_seconds: float = 60.0,
    ) -> None:
        if sample_rate <= 0 or block_size <= 0:
            raise ValueError("sample_rate and block_size must be positive")
        if not 0 <= minimum_seconds < maximum_seconds:
            raise ValueError("recording duration bounds are invalid")
        self._sounddevice = sounddevice
        self._input_device = input_device
        self._sample_rate = sample_rate
        self._block_size = block_size
        self._minimum_samples = int(sample_rate * minimum_seconds)
        self._maximum_samples = int(sample_rate * maximum_seconds)
        self._maximum_seconds = maximum_seconds
        self._lock = Lock()
        self._stream: Any | None = None
        self._limit_timer: Timer | None = None
        self._chunks: list[bytes] = []
        self._sample_count = 0
        self._recording = False
        self._limit_reached = False

    def start(self) -> None:
        with self._lock:
            if self._recording:
                raise RuntimeError("Already recording")
            self._chunks = []
            self._sample_count = 0
            self._recording = True
            self._limit_reached = False

        sounddevice = self._sounddevice or self._load_sounddevice()
        stream: Any | None = None
        try:
            stream = sounddevice.RawInputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self._block_size,
                callback=self._capture,
                device=self._input_device,
            )
            with self._lock:
                self._stream = stream
            stream.start()
        except Exception:
            with self._lock:
                self._recording = False
                self._stream = None
                self._chunks = []
                self._sample_count = 0
            try:
                self._close_stream(stream, abort=True)
            except Exception:
                logger.debug("Failed to close microphone after startup failure", exc_info=True)
            raise

        timer = Timer(self._maximum_seconds, self._stop_at_limit)
        timer.daemon = True
        with self._lock:
            if self._recording and self._stream is stream:
                self._limit_timer = timer
                timer.start()

    def _capture(self, input_data: Any, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            logger.warning("Microphone status: %s", status)
        chunk = bytes(input_data)
        with self._lock:
            if not self._recording:
                return
            remaining_samples = self._maximum_samples - self._sample_count
            if remaining_samples <= 0:
                return
            chunk = chunk[: remaining_samples * SAMPLE_WIDTH_BYTES]
            self._chunks.append(chunk)
            self._sample_count += len(chunk) // SAMPLE_WIDTH_BYTES

    def stop(self) -> np.ndarray:
        with self._lock:
            if not self._recording:
                raise RuntimeError("Not recording")
            stream = self._stream
            timer = self._limit_timer
            chunks = self._chunks
            sample_count = self._sample_count
            limit_reached = self._limit_reached
            self._stream = None
            self._limit_timer = None
            self._chunks = []
            self._sample_count = 0
            self._recording = False
            self._limit_reached = False

        if timer is not None:
            timer.cancel()
        self._close_stream(stream)
        if limit_reached:
            raise RuntimeError("Recording reached the maximum duration and was discarded")
        if sample_count < self._minimum_samples:
            raise RuntimeError("Recording is too short; speak for a little longer")

        pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
        return (pcm.astype(np.float32) / 32768.0).copy()

    def cancel(self) -> None:
        with self._lock:
            stream = self._stream
            timer = self._limit_timer
            self._stream = None
            self._limit_timer = None
            self._chunks = []
            self._sample_count = 0
            self._recording = False
            self._limit_reached = False
        if timer is not None:
            timer.cancel()
        self._close_stream(stream, abort=True)

    def _stop_at_limit(self) -> None:
        with self._lock:
            if not self._recording:
                return
            stream = self._stream
            self._stream = None
            self._limit_timer = None
            self._limit_reached = True
        try:
            self._close_stream(stream, abort=True)
        except Exception:
            logger.debug("Failed to close microphone at the recording limit", exc_info=True)

    @staticmethod
    def _close_stream(stream: Any | None, *, abort: bool = False) -> None:
        if stream is None:
            return
        try:
            if abort and callable(getattr(stream, "abort", None)):
                stream.abort()
            else:
                stream.stop()
        finally:
            stream.close()

    @staticmethod
    def _load_sounddevice() -> Any:
        import sounddevice

        return sounddevice


class ParakeetTranscriber:
    """Lazy MLX adapter for NVIDIA Parakeet TDT on Apple Silicon."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            raise RuntimeError("Pi Voice currently requires Apple Silicon macOS")

        import mlx.core as mx

        model = self._get_model()
        result = model.decode_chunk(mx.array(audio, dtype=mx.float32), verbose=False)
        text = result.text if isinstance(getattr(result, "text", None), str) else str(result)
        language = getattr(result, "language", None)
        return TranscriptionResult(text=text, language_code=language if isinstance(language, str) else None)

    def cleanup(self) -> None:
        self._model = None

    def _get_model(self) -> Any:
        if self._model is None:
            from mlx_audio.stt.generate import load_model

            logger.info("Loading local Parakeet model: %s", self._model_name)
            self._model = load_model(self._model_name)
        return self._model


class LocalSpeechRuntime:
    """Own the microphone and lazily reuse one local transcriber."""

    def __init__(
        self,
        *,
        recorder: Recorder | None = None,
        sounddevice: Any | None = None,
        transcriber_factory: Callable[[], Transcriber] | None = None,
    ) -> None:
        self._recorder = recorder or AudioRecorder(sounddevice=sounddevice)
        self._transcriber_factory = transcriber_factory or ParakeetTranscriber
        self._transcriber: Transcriber | None = None
        self._operation_lock = Lock()
        self._shutdown = False

    def start_recording(self) -> None:
        self._ensure_running()
        self._recorder.start()

    def stop_recording(self) -> object:
        self._ensure_running()
        return self._recorder.stop()

    def transcribe(self, audio: object) -> TranscriptionResult:
        self._ensure_running()
        waveform = np.asarray(audio, dtype=np.float32).squeeze()
        if waveform.ndim != 1 or waveform.size == 0:
            raise RuntimeError("Recording did not contain mono audio")

        with self._operation_lock:
            return self._get_transcriber().transcribe(waveform)

    def cancel(self) -> None:
        self._recorder.cancel()

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.cancel()
        with self._operation_lock:
            if self._transcriber is not None:
                self._transcriber.cleanup()
            self._transcriber = None

    def _get_transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = self._transcriber_factory()
        return self._transcriber

    def _ensure_running(self) -> None:
        if self._shutdown:
            raise RuntimeError("Voice runtime is shut down")
