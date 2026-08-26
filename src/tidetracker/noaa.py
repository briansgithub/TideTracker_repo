"""Bounded NOAA predictions client and strict response validation."""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .deadlines import run_with_wall_clock_deadline
from .models import JSONValue, StationConfig, TidePrediction
from .storage import atomic_write_bytes

NOAA_ENDPOINT = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


class NoaaError(RuntimeError):
    pass


class NoaaTransportError(NoaaError):
    pass


class NoaaResponseError(NoaaError):
    pass


class PredictionValidationError(NoaaError):
    pass


class NoaaFaultConfigurationError(NoaaError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class FaultTelemetry(Protocol):
    def emit(self, event: str, **fields: JSONValue) -> None: ...


NOAA_FAULT_KINDS = frozenset({"dns", "tls", "http_503"})


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_fault(
    path: Path,
    *,
    required_uid: int = 0,
    required_mode: int = 0o600,
) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise NoaaFaultConfigurationError(f"cannot inspect NOAA fault file: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise NoaaFaultConfigurationError("NOAA fault file must be a regular file")
    if metadata.st_uid != required_uid or stat.S_IMODE(metadata.st_mode) != required_mode:
        raise NoaaFaultConfigurationError("NOAA fault file must be root-owned mode 0600")
    if metadata.st_size > 16:
        raise NoaaFaultConfigurationError("NOAA fault file is too large")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NoaaFaultConfigurationError(f"cannot read NOAA fault file: {exc}") from exc
    accepted = {
        value: kind
        for kind in NOAA_FAULT_KINDS
        for value in (kind.encode("ascii"), f"{kind}\n".encode("ascii"))
    }
    try:
        return accepted[raw]
    except KeyError as exc:
        raise NoaaFaultConfigurationError("NOAA fault kind is not allowed") from exc


def stage_noaa_fault_request(
    request_path: str | Path,
    runtime_path: str | Path,
    *,
    required_uid: int = 0,
    required_mode: int = 0o600,
) -> str | None:
    """Consume a persistent cold-cycle request into a RAM-backed marker."""

    request = Path(request_path)
    runtime = Path(runtime_path)
    consuming = request.with_name(f".{request.name}.consuming")
    if runtime.exists():
        if request.exists() or consuming.exists():
            raise NoaaFaultConfigurationError("a NOAA fault is already staged")
        return _read_fault(
            runtime, required_uid=required_uid, required_mode=required_mode
        )
    if request.exists() and consuming.exists():
        raise NoaaFaultConfigurationError("multiple NOAA fault requests are present")
    source = consuming if consuming.exists() else request
    if not source.exists():
        return None
    kind = _read_fault(
        source, required_uid=required_uid, required_mode=required_mode
    )
    if source == request:
        try:
            os.replace(request, consuming)
            _fsync_directory(request.parent)
        except OSError as exc:
            raise NoaaFaultConfigurationError(f"cannot consume NOAA fault request: {exc}") from exc
    atomic_write_bytes(runtime, f"{kind}\n".encode("ascii"), mode=0o600)
    try:
        consuming.unlink()
        _fsync_directory(consuming.parent)
    except OSError as exc:
        with suppress(OSError):
            runtime.unlink()
        raise NoaaFaultConfigurationError(f"cannot retire NOAA fault request: {exc}") from exc
    return kind


class FaultInjectingTransport:
    """Apply a staged one-shot HIL failure only when the HTTPS fetch begins."""

    def __init__(
        self,
        transport: HttpTransport,
        runtime_path: str | Path,
        *,
        telemetry: FaultTelemetry | None = None,
        required_uid: int = 0,
        required_mode: int = 0o600,
    ) -> None:
        self.transport = transport
        self.runtime_path = Path(runtime_path)
        self.telemetry = telemetry
        self.required_uid = required_uid
        self.required_mode = required_mode
        self.fault_kind: str | None = None

    def _consume(self) -> str | None:
        if not self.runtime_path.exists():
            return None
        kind = _read_fault(
            self.runtime_path,
            required_uid=self.required_uid,
            required_mode=self.required_mode,
        )
        try:
            self.runtime_path.unlink()
            _fsync_directory(self.runtime_path.parent)
        except OSError as exc:
            raise NoaaFaultConfigurationError(
                f"cannot consume staged NOAA fault: {exc}"
            ) from exc
        self.fault_kind = kind
        if self.telemetry is not None:
            self.telemetry.emit("noaa_fault_injected", fault_kind=kind)
        return kind

    def get(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        kind = self._consume()
        if kind == "dns":
            raise NoaaTransportError("injected NOAA DNS failure")
        if kind == "tls":
            raise NoaaTransportError("injected NOAA TLS failure")
        if kind == "http_503":
            return HttpResponse(503, b'{"error":{"message":"injected"}}', {})
        return self.transport.get(
            url,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


class UrlLibTransport:
    """Stdlib transport with a caller-visible total deadline and size bound."""

    def __init__(self, *, user_agent: str = "TideTracker/1") -> None:
        self.user_agent = user_agent

    def _request(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        request = Request(
            url, headers={"Accept": "application/json", "User-Agent": self.user_agent}
        )
        # urllib applies this socket timeout to connect and each subsequent
        # blocking read. The outer worker deadline in ``get`` additionally
        # bounds DNS and slow-trickle responses across all operations.
        socket_timeout = min(connect_timeout_seconds, read_timeout_seconds)
        try:
            with urlopen(request, timeout=socket_timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
                chunks: list[bytes] = []
                received = 0
                while True:
                    chunk = response.read(min(65_536, max_response_bytes + 1 - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > max_response_bytes:
                        raise NoaaResponseError(
                            "NOAA response exceeds configured size limit"
                        )
                return HttpResponse(
                    status=int(response.status),
                    body=b"".join(chunks),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except NoaaResponseError:
            raise
        except HTTPError as exc:
            try:
                detail = exc.read(1024).decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            raise NoaaTransportError(f"NOAA HTTP {exc.code}: {detail[:200]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise NoaaTransportError(f"NOAA request failed: {exc}") from exc

    def get(
        self,
        url: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise NoaaTransportError("NOAA timeout budget is exhausted")

        def request() -> HttpResponse:
            return self._request(
                url,
                connect_timeout_seconds=connect_timeout_seconds,
                read_timeout_seconds=read_timeout_seconds,
                max_response_bytes=max_response_bytes,
            )

        try:
            return run_with_wall_clock_deadline(
                request,
                timeout_seconds=read_timeout_seconds,
                timeout_error=NoaaTransportError(
                    "NOAA request exceeded its total wall-clock timeout"
                ),
            )
        except NoaaError:
            raise
        except Exception as exc:
            raise NoaaTransportError("NOAA request failed") from exc


def validate_predictions(
    predictions: tuple[TidePrediction, ...], *, max_predictions: int = 4096
) -> tuple[TidePrediction, ...]:
    if not predictions:
        raise PredictionValidationError("NOAA response contains no predictions")
    if len(predictions) > max_predictions:
        raise PredictionValidationError("NOAA response contains too many predictions")
    previous: datetime | None = None
    for prediction in predictions:
        if prediction.timestamp.tzinfo is None or prediction.timestamp.utcoffset() is None:
            raise PredictionValidationError("prediction timestamp is not timezone-aware")
        if not math.isfinite(prediction.value):
            raise PredictionValidationError("prediction value is not finite")
        if previous is not None and prediction.timestamp <= previous:
            raise PredictionValidationError("prediction timestamps must be strictly increasing")
        previous = prediction.timestamp
    return predictions


def parse_noaa_predictions(
    body: bytes, *, max_predictions: int = 4096
) -> tuple[TidePrediction, ...]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NoaaResponseError("NOAA returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise NoaaResponseError("NOAA response must be a JSON object")
    if "error" in payload:
        error = payload["error"]
        if isinstance(error, dict):
            message = str(error.get("message", "unknown NOAA error"))
        else:
            message = str(error)
        raise NoaaResponseError(f"NOAA rejected request: {message[:300]}")
    raw_predictions = payload.get("predictions")
    if not isinstance(raw_predictions, list):
        raise NoaaResponseError("NOAA response has no predictions list")
    if len(raw_predictions) > max_predictions:
        raise PredictionValidationError("NOAA response contains too many predictions")

    parsed: list[TidePrediction] = []
    for index, item in enumerate(raw_predictions):
        if not isinstance(item, dict) or not isinstance(item.get("t"), str):
            raise NoaaResponseError(f"prediction {index} is malformed")
        try:
            timestamp = datetime.strptime(item["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            value = float(item["v"])
            parsed.append(TidePrediction(timestamp=timestamp, value=value))
        except (KeyError, TypeError, ValueError) as exc:
            raise NoaaResponseError(f"prediction {index} is malformed") from exc
    return validate_predictions(tuple(parsed), max_predictions=max_predictions)


class NoaaClient:
    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        datum: str = "MLLW",
        units: str = "english",
        endpoint: str = NOAA_ENDPOINT,
        max_response_bytes: int = 1_048_576,
        max_predictions: int = 4096,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not endpoint.lower().startswith("https://"):
            raise ValueError("NOAA endpoint must use HTTPS")
        if max_response_bytes <= 0 or max_predictions <= 0:
            raise ValueError("NOAA response bounds must be positive")
        self.transport = transport or UrlLibTransport()
        self.datum = datum.upper()
        self.units = units
        self.endpoint = endpoint
        self.max_response_bytes = max_response_bytes
        self.max_predictions = max_predictions
        self.monotonic = monotonic

    @property
    def fault_kind(self) -> str | None:
        value = getattr(self.transport, "fault_kind", None)
        return value if isinstance(value, str) else None

    def fetch(
        self,
        station: StationConfig,
        *,
        begin: date,
        range_hours: int,
        interval_minutes: int,
        timeout_seconds: float,
    ) -> tuple[TidePrediction, ...]:
        if timeout_seconds <= 0:
            raise NoaaTransportError("NOAA timeout budget is exhausted")
        if not 1 <= range_hours <= 240:
            raise ValueError("range_hours must be between 1 and 240")
        if interval_minutes not in {1, 5, 6, 10, 15, 30, 60}:
            raise ValueError("unsupported NOAA prediction interval")
        query = urlencode(
            {
                "begin_date": begin.strftime("%Y%m%d"),
                "range": str(range_hours),
                "product": "predictions",
                "datum": self.datum,
                "interval": str(interval_minutes),
                "format": "json",
                "units": self.units,
                # GMT makes repeated DST hours unambiguous; rendering converts
                # these aware timestamps through the station's ZoneInfo.
                "time_zone": "gmt",
                "station": station.station_id,
                "application": "TideTracker",
            }
        )
        started = self.monotonic()
        connect_timeout = min(5.0, timeout_seconds)
        response = self.transport.get(
            f"{self.endpoint}?{query}",
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=timeout_seconds,
            max_response_bytes=self.max_response_bytes,
        )
        elapsed = self.monotonic() - started
        if elapsed > timeout_seconds:
            raise NoaaTransportError("NOAA request exceeded its total timeout budget")
        if not 200 <= response.status < 300:
            raise NoaaTransportError(f"NOAA returned HTTP {response.status}")
        return parse_noaa_predictions(response.body, max_predictions=self.max_predictions)
