"""Structured, secret-redacting JSONL telemetry."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .deadlines import run_with_wall_clock_deadline
from .models import JSONValue

_SECRET_FRAGMENTS = (
    "password",
    "passwd",
    "psk",
    "secret",
    "credential",
    "token",
    "private_key",
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|psk|secret|credential|token|private[_ -]?key)\b"
    r"\s*(?:=|:)\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_text(value: str) -> str:
    """Redact common secret assignments embedded in exception text."""

    return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)


def _safe_value(value: Any, key: str = "") -> JSONValue:
    if any(fragment in key.lower() for fragment in _SECRET_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dict):
        return {
            str(child_key): _safe_value(child, str(child_key)) for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(child) for child in value]
    return str(value)


class JsonlTelemetry:
    """Append complete, fsynced JSON events using a single O_APPEND write."""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        fsync_each_event: bool = True,
        max_bytes: int = 1_048_576,
        backup_count: int = 2,
        io_timeout_seconds: float = 0.25,
    ) -> None:
        if max_bytes <= 0 or backup_count < 0 or io_timeout_seconds <= 0:
            raise ValueError("telemetry retention bounds are invalid")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(UTC))
        self._fsync_each_event = fsync_each_event
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._io_timeout_seconds = io_timeout_seconds
        self._lock = threading.Lock()

    def _rotate(self) -> None:
        if not self.path.exists():
            return
        if self._backup_count == 0:
            self.path.unlink()
        else:
            oldest = self.path.with_name(f"{self.path.name}.{self._backup_count}")
            with suppress(FileNotFoundError):
                oldest.unlink()
            for index in range(self._backup_count - 1, 0, -1):
                source = self.path.with_name(f"{self.path.name}.{index}")
                target = self.path.with_name(f"{self.path.name}.{index + 1}")
                with suppress(FileNotFoundError):
                    os.replace(source, target)
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        try:
            descriptor = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def emit(self, event: str, **fields: JSONValue) -> None:
        if not event or not event.strip():
            raise ValueError("telemetry event name must not be empty")
        record: dict[str, JSONValue] = {
            "timestamp": self._now().isoformat(),
            "event": event,
        }
        record.update({key: _safe_value(value, key) for key, value in fields.items()})
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > self._max_bytes:
            raise ValueError("telemetry event exceeds the retained file bound")
        def append() -> None:
            with self._lock:
                try:
                    current_size = self.path.stat().st_size
                except FileNotFoundError:
                    current_size = 0
                if current_size and current_size + len(payload) > self._max_bytes:
                    self._rotate()
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("telemetry append made no progress")
                        remaining = remaining[written:]
                    if self._fsync_each_event:
                        os.fsync(descriptor)
                finally:
                    os.close(descriptor)

        run_with_wall_clock_deadline(
            append,
            timeout_seconds=self._io_timeout_seconds,
            timeout_error=TimeoutError(
                "telemetry append exceeded its wall-clock deadline"
            ),
        )


class TelemetryLineWriter(Protocol):
    def write_line(self, payload: bytes) -> None: ...

    def close(self) -> None: ...


class SerialLineWriter:
    """Lazily open and drain a Linux UART without a shell or buffering daemon."""

    def __init__(self, device: str | Path, *, baudrate: int = 115_200) -> None:
        if baudrate != 115_200:
            raise ValueError("only the fixture's fixed 115200 baud rate is supported")
        self.device = Path(device)
        self._descriptor: int | None = None
        self._termios: Any = None

    def _open(self) -> int:
        if self._descriptor is not None:
            return self._descriptor
        descriptor: int | None = None
        try:
            termios = __import__("termios")
            tty = __import__("tty")
            descriptor = os.open(
                self.device,
                os.O_WRONLY
                | getattr(os, "O_NOCTTY", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            tty.setraw(descriptor)
            attributes = termios.tcgetattr(descriptor)
            attributes[4] = termios.B115200
            attributes[5] = termios.B115200
            attributes[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
        except BaseException:
            if descriptor is not None:
                with suppress(Exception):
                    os.close(descriptor)
            raise
        assert descriptor is not None
        self._descriptor = descriptor
        self._termios = termios
        return descriptor

    def write_line(self, payload: bytes) -> None:
        descriptor = self._open()
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("UART telemetry write was incomplete")
        self._termios.tcdrain(descriptor)

    def close(self) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


class UartPhaseTelemetry:
    """Emit only bounded, secret-free fixture evidence as ASCII JSON lines."""

    _EVENTS = frozenset(
        {"phase_started", "phase_finished", "target_identity", "update_phase_boundary"}
    )
    _SHA256 = re.compile(r"[0-9a-f]{64}")
    _SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    MAX_LINE_BYTES = 512

    def __init__(
        self,
        writer: TelemetryLineWriter,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.writer = writer
        self.monotonic_ns = monotonic_ns

    def emit(self, event: str, **fields: JSONValue) -> None:
        if event not in self._EVENTS:
            return
        record: dict[str, JSONValue] = {"schema_version": 1, "event": event}
        if event in {"phase_started", "phase_finished"}:
            record.update(
                {
                    "run_id": redact_text(str(fields.get("run_id", "")))[:64],
                    "phase": redact_text(str(fields.get("phase", "")))[:24],
                    "monotonic_ns": max(0, int(self.monotonic_ns())),
                }
            )
        elif event == "target_identity":
            release_id = str(fields.get("release_id", ""))
            execution_source = str(fields.get("execution_source", ""))
            image_build_id = str(fields.get("image_build_id", ""))
            if self._SAFE_ID.fullmatch(release_id) is None:
                raise ValueError("UART target release ID is invalid")
            if execution_source not in {"built-in", "verified-release"}:
                raise ValueError("UART target execution source is invalid")
            if self._SHA256.fullmatch(image_build_id) is None:
                raise ValueError("UART image build ID is invalid")
            record.update(
                {
                    "release_id": release_id,
                    "execution_source": execution_source,
                    "image_build_id": image_build_id,
                }
            )
        else:
            phase = str(fields.get("phase", ""))
            release_id = str(fields.get("release_id", ""))
            bundle_sha256 = str(fields.get("bundle_sha256", ""))
            if phase not in {"begin", "stage", "verify", "activate"}:
                raise ValueError("UART update phase is invalid")
            if self._SAFE_ID.fullmatch(release_id) is None:
                raise ValueError("UART update release ID is invalid")
            if self._SHA256.fullmatch(bundle_sha256) is None:
                raise ValueError("UART update bundle SHA-256 is invalid")
            record.update(
                {
                    "phase": phase,
                    "release_id": release_id,
                    "bundle_sha256": bundle_sha256,
                }
            )
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
        if len(payload) > self.MAX_LINE_BYTES:
            raise ValueError("UART telemetry line exceeds its fixed bound")
        self.writer.write_line(payload)

    def close(self) -> None:
        self.writer.close()


class CompositeTelemetry:
    """Fan out best-effort telemetry; one failed sink never blocks another."""

    def __init__(self, *sinks: Any) -> None:
        self.sinks = sinks

    def emit(self, event: str, **fields: JSONValue) -> None:
        for sink in self.sinks:
            with suppress(Exception):
                sink.emit(event, **fields)

    def close(self) -> None:
        for sink in self.sinks:
            close = getattr(sink, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()


class MemoryTelemetry:
    def __init__(self) -> None:
        self.events: list[dict[str, JSONValue]] = []

    def emit(self, event: str, **fields: JSONValue) -> None:
        self.events.append({"event": event, **fields})


class NullTelemetry:
    def emit(self, event: str, **fields: JSONValue) -> None:
        del event, fields
