"""Small, dependency-free captive portal for TideTracker setup mode."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .config import ConfigError, default_config, load_config, save_config
from .models import AppConfig, CycleMode, StationConfig
from .models import NetworkStatus as RuntimeNetworkStatus
from .network import NetworkError, NetworkManagerAdapter, NetworkProfile
from .stations import StationCatalog, default_catalog_path

MAX_REQUEST_BYTES = 64 * 1024
SECRET_KEYS = frozenset(
    {"credential", "credentials", "identity", "passphrase", "password", "psk", "secret", "username"}
)
PUBLIC_CONFIG_KEYS = frozenset(
    {"station_id", "datum", "units", "prediction_interval_minutes", "chart_hours"}
)


class PortalError(RuntimeError):
    pass


class OperationBusy(PortalError):
    pass


class ConfigStore(Protocol):
    def read_public(self) -> dict[str, object]: ...

    def update_public(self, values: Mapping[str, object]) -> dict[str, object]: ...


class NetworkService(Protocol):
    def scan(self) -> tuple[Any, ...]: ...

    def status(self) -> Any: ...

    def test_and_save(self, profile: NetworkProfile) -> dict[str, object]: ...

    def force_save(self, profile: NetworkProfile) -> dict[str, object]: ...


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in SECRET_KEYS:
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _validate_config(values: Mapping[str, object]) -> dict[str, object]:
    unknown = set(values) - PUBLIC_CONFIG_KEYS
    if unknown:
        raise ValueError("configuration contains unsupported fields")
    if _contains_secret_key(values):
        raise ValueError("credentials cannot be stored in application configuration")

    result: dict[str, object] = {}
    if "station_id" in values:
        station_id = values["station_id"]
        if not isinstance(station_id, str) or not station_id.isdigit() or len(station_id) != 7:
            raise ValueError("station_id must be a seven-digit NOAA station ID")
        result["station_id"] = station_id
    if "datum" in values:
        datum = str(values["datum"]).upper()
        if datum not in {"MLLW", "MLW", "MSL", "MTL", "MHW", "MHHW", "NAVD"}:
            raise ValueError("unsupported NOAA datum")
        result["datum"] = datum
    if "units" in values:
        units = str(values["units"]).lower()
        if units not in {"english", "metric"}:
            raise ValueError("units must be english or metric")
        result["units"] = units
    if "prediction_interval_minutes" in values:
        interval = values["prediction_interval_minutes"]
        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval not in {1, 5, 6, 10, 15, 30, 60}
        ):
            raise ValueError("prediction interval is unsupported")
        result["prediction_interval_minutes"] = interval
    if "chart_hours" in values:
        chart_hours = values["chart_hours"]
        if not isinstance(chart_hours, int) or isinstance(chart_hours, bool) or not 1 <= chart_hours <= 48:
            raise ValueError("chart_hours must be between 1 and 48")
        result["chart_hours"] = chart_hours
    return result


class AtomicJSONConfigStore:
    """Portal view over the versioned application configuration.

    Only the small public subset is accepted from HTTP.  Writes are resolved to a
    complete :class:`AppConfig`, including station coordinates/timezone and all
    untouched timeouts, before the core atomic config writer is used.
    """

    def __init__(
        self,
        path: Path,
        defaults: Mapping[str, object] | AppConfig | None = None,
        *,
        catalog: StationCatalog | None = None,
    ) -> None:
        self.path = Path(path)
        self._catalog = catalog
        self._lock = threading.RLock()
        if isinstance(defaults, AppConfig):
            self._defaults = defaults
        elif defaults is None:
            self._defaults = default_config()
        else:
            self._defaults = self._apply_public(default_config(), _validate_config(defaults))

    def _station(self, station_id: str, current: StationConfig) -> StationConfig:
        if station_id == current.station_id:
            return current
        if self._catalog is None:
            self._catalog = StationCatalog.from_csv(default_catalog_path())
        station = self._catalog.get(station_id)
        return StationConfig(
            station_id=station.station_id,
            name=f"{station.city}, {station.state}",
            latitude=station.latitude,
            longitude=station.longitude,
            timezone=station.timezone,
        )

    def _apply_public(self, config: AppConfig, values: Mapping[str, object]) -> AppConfig:
        station = config.station
        if "station_id" in values:
            station = self._station(str(values["station_id"]), station)
        return replace(
            config,
            station=station,
            datum=str(values.get("datum", config.datum)),
            units=str(values.get("units", config.units)),
            prediction_interval_minutes=cast(
                int,
                values.get("prediction_interval_minutes", config.prediction_interval_minutes)
            ),
            chart_hours=cast(int, values.get("chart_hours", config.chart_hours)),
        )

    @staticmethod
    def _public(config: AppConfig) -> dict[str, object]:
        return {
            "station_id": config.station.station_id,
            "datum": config.datum,
            "units": config.units,
            "prediction_interval_minutes": config.prediction_interval_minutes,
            "chart_hours": config.chart_hours,
        }

    def _load(self) -> AppConfig:
        if not self.path.exists():
            return self._defaults
        try:
            return load_config(self.path)
        except ConfigError:
            # Read the one legacy flat shape solely to permit an in-place migration
            # on the next portal save.  Credential-bearing legacy objects are refused.
            try:
                legacy = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(legacy, dict) or _contains_secret_key(legacy):
                    raise ValueError
                station_value = legacy.get("station_id")
                if not isinstance(station_value, str):
                    raise ValueError
                station_id = station_value.split(" - ", 1)[0].strip()
                public = _validate_config({"station_id": station_id})
                return self._apply_public(self._defaults, public)
            except (OSError, ValueError, json.JSONDecodeError) as migration_error:
                raise PortalError("configuration is invalid") from migration_error

    def read_public(self) -> dict[str, object]:
        with self._lock:
            return self._public(self._load())

    def update_public(self, values: Mapping[str, object]) -> dict[str, object]:
        update = _validate_config(values)
        with self._lock:
            try:
                updated = self._apply_public(self._load(), update)
                save_config(self.path, updated)
            except (ConfigError, OSError, ValueError) as exc:
                raise PortalError("configuration could not be saved") from exc
            return self._public(updated)


@dataclass(slots=True)
class OperationRecord:
    operation_id: str
    kind: str
    state: str = "accepted"
    result: dict[str, object] | None = None
    error: str | None = None
    accepted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def public_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "state": self.state,
        }
        if self.result is not None:
            value["result"] = dict(self.result)
        if self.error is not None:
            value["error"] = self.error
        return value


class DeferredOperation:
    def __init__(
        self,
        queue: NetworkOperationQueue,
        operation_id: str,
        task: Callable[[], dict[str, object]],
    ) -> None:
        self.queue = queue
        self.operation_id = operation_id
        self._task = task
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.queue.start(self.operation_id, self._task)

    def cancel(self) -> None:
        if not self._started:
            self.queue.cancel(self.operation_id)


class NetworkOperationQueue:
    """One-at-a-time operation queue whose public records never contain secrets."""

    def __init__(self, executor: Executor | None = None) -> None:
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="portal-net")
        self._lock = threading.RLock()
        self._active: str | None = None
        self._records: dict[str, OperationRecord] = {}
        self._latest: str | None = None

    def reserve(self, kind: str, task: Callable[[], dict[str, object]]) -> DeferredOperation:
        with self._lock:
            if self._active is not None:
                raise OperationBusy("another network operation is already active")
            operation_id = uuid.uuid4().hex
            self._records[operation_id] = OperationRecord(operation_id, kind)
            self._latest = operation_id
            self._active = operation_id
            return DeferredOperation(self, operation_id, task)

    def start(self, operation_id: str, task: Callable[[], dict[str, object]]) -> None:
        with self._lock:
            if self._active != operation_id:
                return
        self._executor.submit(self._execute, operation_id, task)

    def cancel(self, operation_id: str) -> None:
        with self._lock:
            if self._active == operation_id:
                self._records[operation_id].state = "cancelled"
                self._records[operation_id].finished_at = time.time()
                self._active = None

    def _execute(self, operation_id: str, task: Callable[[], dict[str, object]]) -> None:
        with self._lock:
            record = self._records[operation_id]
            record.state = "running"
            record.started_at = time.time()
        try:
            result = task()
        except (NetworkError, ValueError):
            with self._lock:
                record.state = "failed"
                record.error = f"{record.kind}_failed"
        except Exception:
            with self._lock:
                record.state = "failed"
                record.error = "internal_error"
        else:
            with self._lock:
                record.state = "succeeded"
                record.result = dict(result)
        finally:
            with self._lock:
                record.finished_at = time.time()
                if self._active == operation_id:
                    self._active = None

    def public_status(self) -> dict[str, object] | None:
        with self._lock:
            if self._latest is None:
                return None
            return self._records[self._latest].public_dict()

    def close(self) -> None:
        if self._owns_executor:
            assert isinstance(self._executor, ThreadPoolExecutor)
            self._executor.shutdown(wait=True, cancel_futures=False)


@dataclass(frozen=True, slots=True)
class PortalResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)
    after_send: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    on_send_failure: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def json(
        cls,
        status: int,
        value: Mapping[str, object],
        *,
        after_send: Callable[[], None] | None = None,
        on_send_failure: Callable[[], None] | None = None,
    ) -> PortalResponse:
        return cls(
            status,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
            after_send=after_send,
            on_send_failure=on_send_failure,
        )


class PortalApplication:
    def __init__(
        self,
        network: NetworkService,
        config: ConfigStore,
        *,
        operations: NetworkOperationQueue | None = None,
    ) -> None:
        self.network = network
        self.config = config
        self.operations = operations or NetworkOperationQueue()
        self._owns_operations = operations is None
        self.network_saved = threading.Event()
        self._completion_callback: Callable[[], None] | None = None

    def begin_session(self) -> None:
        self.network_saved.clear()

    def set_completion_callback(self, callback: Callable[[], None] | None) -> None:
        self._completion_callback = callback

    def _status(self) -> dict[str, object]:
        try:
            status = self.network.status().public_dict()
            access_points = [item.public_dict() for item in self.network.scan()]
            network_error = None
        except NetworkError:
            status = {"connected": False, "state": "unavailable", "connection": None}
            access_points = []
            network_error = "network_unavailable"
        result: dict[str, object] = {
            "network": status,
            "access_points": access_points,
            "config": self.config.read_public(),
            "operation": self.operations.public_status(),
        }
        if network_error:
            result["error"] = network_error
        return result

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, object]:
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request is too large")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _network_response(
        self,
        kind: str,
        task: Callable[[], dict[str, object]],
        *,
        complete_session: bool,
    ) -> PortalResponse:
        def save_and_finish() -> dict[str, object]:
            result = task()
            self.network_saved.set()
            # A tested profile has already moved wlan0 away from the setup AP,
            # so end the HTTP session after its 202 response is on the wire.
            # Force-save deliberately leaves the setup AP available: it proves
            # only durable storage and must not imply successful association.
            if complete_session and self._completion_callback is not None:
                self._completion_callback()
            return result

        try:
            operation = self.operations.reserve(kind, save_and_finish)
        except OperationBusy:
            return PortalResponse.json(409, {"error": "network_operation_in_progress"})
        return PortalResponse.json(
            202,
            {"operation_id": operation.operation_id, "state": "accepted"},
            after_send=operation.start,
            on_send_failure=operation.cancel,
        )

    def handle(self, method: str, target: str, body: bytes = b"") -> PortalResponse:
        path = urlsplit(target).path
        if method == "GET" and path == "/":
            return PortalResponse(200, PORTAL_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if method == "GET" and path in {
            "/generate_204",
            "/hotspot-detect.html",
            "/connecttest.txt",
            "/ncsi.txt",
        }:
            return PortalResponse(302, b"", headers={"Location": "/"})
        if method == "GET" and path == "/api/status":
            try:
                return PortalResponse.json(200, self._status())
            except PortalError:
                return PortalResponse.json(503, {"error": "configuration_unavailable"})
        if method != "POST":
            return PortalResponse.json(404, {"error": "not_found"})

        try:
            payload = self._decode_json(body)
        except ValueError:
            return PortalResponse.json(400, {"error": "invalid_request"})

        if path == "/api/config":
            if _contains_secret_key(payload):
                return PortalResponse.json(400, {"error": "credentials_not_allowed"})
            try:
                updated = self.config.update_public(payload)
            except (PortalError, ValueError):
                return PortalResponse.json(400, {"error": "invalid_configuration"})
            return PortalResponse.json(200, {"config": updated})

        if path not in {"/api/network/test", "/api/network/force-save"}:
            return PortalResponse.json(404, {"error": "not_found"})
        try:
            profile = NetworkProfile.from_mapping(payload)
        except ValueError:
            return PortalResponse.json(400, {"error": "invalid_network_profile"})
        if path == "/api/network/test":
            return self._network_response(
                "network_test",
                lambda: self.network.test_and_save(profile),
                complete_session=True,
            )
        return self._network_response(
            "network_force_save",
            lambda: self.network.force_save(profile),
            complete_session=False,
        )

    def close(self) -> None:
        if self._owns_operations:
            self.operations.close()


class _PortalRequestHandler(BaseHTTPRequestHandler):
    application: PortalApplication
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Request logging is deliberately disabled.  A malformed client must not
        # be able to smuggle a credential into a log line.
        return

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = MAX_REQUEST_BYTES + 1
        if length < 0 or length > MAX_REQUEST_BYTES:
            response = PortalResponse.json(413, {"error": "request_too_large"})
        else:
            body = self.rfile.read(length) if length else b""
            response = self.application.handle(self.command, self.path, body)

        sent = False
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)
            self.wfile.flush()
            sent = True
        except (BrokenPipeError, ConnectionError):
            if response.on_send_failure is not None:
                response.on_send_failure()
        finally:
            if sent and response.after_send is not None:
                response.after_send()

    do_GET = _dispatch
    do_POST = _dispatch


def _handler_for(application: PortalApplication) -> type[_PortalRequestHandler]:
    class Handler(_PortalRequestHandler):
        pass

    Handler.application = application
    return Handler


class PortalServer:
    """Lifecycle wrapper suitable for the setup-mode CLI and tests."""

    def __init__(
        self,
        application: PortalApplication,
        address: tuple[str, int] = ("0.0.0.0", 80),
    ) -> None:
        self.application = application
        self._server = ThreadingHTTPServer(address, _handler_for(application))
        self._server.daemon_threads = True
        self.application.set_completion_callback(self.shutdown)

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def serve_for(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("portal timeout must be positive")
        timer = threading.Timer(timeout_seconds, self.shutdown)
        timer.daemon = True
        timer.start()
        try:
            self.serve_forever()
        finally:
            timer.cancel()

    def shutdown(self) -> None:
        self._server.shutdown()

    def close(self) -> None:
        self.application.set_completion_callback(None)
        self._server.server_close()
        self.application.close()


class BoundedPortalServer(Protocol):
    def serve_for(self, timeout_seconds: float) -> None: ...

    def close(self) -> None: ...


class PortalNetworkPort:
    """Cycle network port that separates client mode from a bounded setup AP."""

    def __init__(
        self,
        network: NetworkManagerAdapter,
        application: PortalApplication,
        *,
        hotspot_ssid: str = "TideTracker-Setup",
        gateway: str = "192.168.42.1",
        port: int = 80,
        server_factory: Callable[
            [PortalApplication, tuple[str, int]], BoundedPortalServer
        ] = PortalServer,
    ) -> None:
        self.network = network
        self.application = application
        self.hotspot_ssid = hotspot_ssid
        self.gateway = gateway
        self.port = port
        self.server_factory = server_factory

    def prepare(self, mode: CycleMode, *, timeout_seconds: float) -> RuntimeNetworkStatus:
        if mode is CycleMode.RUN:
            return self.network.prepare_client(timeout_seconds=timeout_seconds)
        if mode is not CycleMode.SETUP:
            raise ValueError("unsupported cycle mode")
        self.application.begin_session()
        session = self.network.start_hotspot(
            self.hotspot_ssid,
            address=f"{self.gateway}/24",
        )
        server = self.server_factory(self.application, (self.gateway, self.port))
        try:
            server.serve_for(timeout_seconds)
        finally:
            try:
                server.close()
            finally:
                self.network.stop_hotspot(
                    session,
                    restore=not self.application.network_saved.is_set(),
                )
        return RuntimeNetworkStatus(connected=False, interface=self.network.interface)

    def release(self, *, timeout_seconds: float) -> None:
        self.network.release_client(timeout_seconds=timeout_seconds)


PORTAL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TideTracker setup</title>
<style>
:root{font-family:system-ui,sans-serif;color:#14213d;background:#eef4f7}body{margin:0}
main{max-width:42rem;margin:auto;padding:1rem}section{background:white;border-radius:.8rem;padding:1rem;
margin:0 0 1rem;box-shadow:0 2px 12px #1232}label{display:block;margin:.8rem 0 .25rem}
input,select,button{box-sizing:border-box;width:100%;font:inherit;padding:.75rem;border:1px solid #789;
border-radius:.45rem}button{margin-top:.7rem;background:#075985;color:white;border:0;font-weight:700}
button.secondary{background:#475569}.row{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
#message{min-height:1.4em;font-weight:600}.hidden{display:none}@media(max-width:30rem){.row{display:block}}
</style></head><body><main><h1>TideTracker setup</h1><p id="message">Loading status…</p>
<section><h2>Location</h2><label for="station">NOAA station ID</label>
<input id="station" inputmode="numeric" pattern="[0-9]{7}" maxlength="7">
<button id="save-station">Save station</button></section>
<section><h2>Wi-Fi</h2><label for="ssid">Network name</label>
<input id="ssid" list="networks" maxlength="32"><datalist id="networks"></datalist>
<label><input id="hidden" type="checkbox" style="width:auto"> Hidden network</label>
<label for="security">Security</label><select id="security"><option value="open">Open</option>
<option value="wpa-personal">WPA/WPA2 Personal</option><option value="wpa-enterprise">WPA Enterprise</option></select>
<div id="enterprise" class="hidden"><label for="identity">Enterprise username</label><input id="identity" autocomplete="username"></div>
<div id="credential" class="hidden"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password"></div>
<div class="row"><button id="test">Test and save</button><button id="force" class="secondary">Save without test</button></div>
</section></main><script>
const $=id=>document.getElementById(id),msg=t=>$('message').textContent=t;
function fields(){return{ssid:$('ssid').value,hidden:$('hidden').checked,security:$('security').value,
identity:$('security').value==='wpa-enterprise'?$('identity').value:null,
password:$('security').value==='open'?null:$('password').value}}
async function post(path,value){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
let data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data}
async function refresh(){try{let s=await(await fetch('/api/status',{cache:'no-store'})).json();
$('station').value=s.config.station_id||'';$('networks').replaceChildren(...s.access_points.map(n=>Object.assign(document.createElement('option'),{value:n.ssid})));
let op=s.operation;msg(op?`Wi-Fi: ${op.state}`:(s.network.connected?'Connected':'Ready for setup'))}catch(e){msg('Status unavailable')}}
$('security').onchange=()=>{let e=$('security').value;$('credential').classList.toggle('hidden',e==='open');$('enterprise').classList.toggle('hidden',e!=='wpa-enterprise')};
$('save-station').onclick=async()=>{try{await post('/api/config',{station_id:$('station').value});msg('Station saved')}catch(e){msg(e.message)}};
async function wifi(path){try{let r=await post(path,fields());msg(`Wi-Fi: ${r.state}`);setTimeout(refresh,1000)}catch(e){msg(e.message)}}
$('test').onclick=()=>wifi('/api/network/test');$('force').onclick=()=>wifi('/api/network/force-save');$('security').onchange();refresh();
</script></body></html>"""


def default_application(
    config_path: Path = Path("/var/lib/tidetracker/config.json"),
) -> PortalApplication:
    return PortalApplication(NetworkManagerAdapter(), AtomicJSONConfigStore(config_path))


__all__ = [
    "AtomicJSONConfigStore",
    "ConfigStore",
    "DeferredOperation",
    "NetworkOperationQueue",
    "OperationBusy",
    "PortalApplication",
    "PortalError",
    "PortalNetworkPort",
    "PortalResponse",
    "PortalServer",
    "default_application",
]
