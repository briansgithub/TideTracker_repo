from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import Any

from tidetracker.config import load_config
from tidetracker.models import CycleMode
from tidetracker.models import NetworkStatus as RuntimeNetworkStatus
from tidetracker.network import AccessPoint, NetworkError, NetworkStatus, SecurityMode
from tidetracker.portal import (
    AtomicJSONConfigStore,
    NetworkOperationQueue,
    PortalApplication,
    PortalNetworkPort,
    PortalServer,
)


class InlineExecutor(Executor):
    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class FakeNetwork:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    def scan(self) -> tuple[AccessPoint, ...]:
        return (
            AccessPoint("Guest", SecurityMode.OPEN, 55),
            AccessPoint("Home", SecurityMode.WPA_PERSONAL, 80),
        )

    def status(self) -> NetworkStatus:
        return NetworkStatus(True, "100 (connected)", "Home")

    def test_and_save(self, profile: Any) -> dict[str, object]:
        self.calls.append(("test", profile.ssid))
        if self.failure:
            raise self.failure
        return profile.public_dict()

    def force_save(self, profile: Any) -> dict[str, object]:
        self.calls.append(("force", profile.ssid))
        if self.failure:
            raise self.failure
        return profile.public_dict()


def make_app(tmp_path: Path, network: FakeNetwork) -> PortalApplication:
    store = AtomicJSONConfigStore(tmp_path / "config.json", {"station_id": "8531833"})
    queue = NetworkOperationQueue(InlineExecutor())
    return PortalApplication(network, store, operations=queue)


def test_network_transition_is_deferred_until_response_callback(tmp_path: Path) -> None:
    network = FakeNetwork()
    app = make_app(tmp_path, network)
    completed: list[str] = []
    app.set_completion_callback(lambda: completed.append("test"))
    body = json.dumps(
        {"ssid": "Home", "security": "wpa-personal", "password": "private-value"}
    ).encode()

    response = app.handle("POST", "/api/network/test", body)

    assert response.status == 202
    assert network.calls == []
    assert b"private-value" not in response.body
    assert response.after_send is not None
    response.after_send()
    assert network.calls == [("test", "Home")]
    assert app.network_saved.is_set()
    assert completed == ["test"]
    operation = app.operations.public_status()
    assert operation is not None
    assert operation["state"] == "succeeded"
    assert "private-value" not in json.dumps(operation)


def test_force_save_keeps_setup_session_available_after_durable_save(tmp_path: Path) -> None:
    network = FakeNetwork()
    app = make_app(tmp_path, network)
    completed: list[str] = []
    app.set_completion_callback(lambda: completed.append("unexpected"))
    response = app.handle(
        "POST",
        "/api/network/force-save",
        b'{"ssid":"Home","security":"wpa-personal","password":"private-value"}',
    )

    assert response.status == 202
    assert response.after_send is not None
    response.after_send()

    assert network.calls == [("force", "Home")]
    assert app.network_saved.is_set()
    assert completed == []
    operation = app.operations.public_status()
    assert operation is not None
    assert operation["state"] == "succeeded"


def test_failed_network_operation_never_exposes_exception_or_secret(tmp_path: Path) -> None:
    network = FakeNetwork()
    network.failure = NetworkError("driver echoed super-secret")
    app = make_app(tmp_path, network)
    response = app.handle(
        "POST",
        "/api/network/test",
        b'{"ssid":"Home","security":"wpa-personal","password":"super-secret"}',
    )

    assert response.after_send is not None
    response.after_send()

    public = app.operations.public_status()
    assert public is not None
    assert public["state"] == "failed"
    assert public["error"] == "network_test_failed"
    assert "super-secret" not in json.dumps(public)


def test_config_endpoint_rejects_credentials_and_writes_public_values_atomically(
    tmp_path: Path,
) -> None:
    network = FakeNetwork()
    app = make_app(tmp_path, network)

    rejected = app.handle(
        "POST", "/api/config", b'{"station_id":"8531833","password":"never-store"}'
    )
    accepted = app.handle(
        "POST",
        "/api/config",
        b'{"station_id":"8518750","datum":"mllw","units":"english","chart_hours":2}',
    )

    assert rejected.status == 400
    assert accepted.status == 200
    config_text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "never-store" not in config_text
    loaded = load_config(tmp_path / "config.json")
    assert loaded.station.station_id == "8518750"
    assert loaded.station.name == "The Battery, NY"
    assert loaded.station.timezone == "America/New_York"
    assert loaded.timeouts.normal_cycle_seconds == 180
    assert not list(tmp_path.glob("*.tmp"))


def test_busy_network_request_is_rejected_without_retaining_second_secret(tmp_path: Path) -> None:
    network = FakeNetwork()
    app = make_app(tmp_path, network)
    first = app.handle(
        "POST",
        "/api/network/test",
        b'{"ssid":"First","security":"wpa-personal","password":"first-secret"}',
    )
    second = app.handle(
        "POST",
        "/api/network/force-save",
        b'{"ssid":"Second","security":"wpa-personal","password":"second-secret"}',
    )

    assert first.status == 202
    assert second.status == 409
    assert b"second-secret" not in second.body
    assert first.on_send_failure is not None
    first.on_send_failure()


def test_stdlib_http_server_exposes_mobile_ui_and_api(tmp_path: Path) -> None:
    network = FakeNetwork()
    app = make_app(tmp_path, network)
    server = PortalServer(app, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode()
        assert response.status == 200
        assert 'name="viewport"' in html
        assert "WPA Enterprise" in html

        connection.request("GET", "/api/status")
        response = connection.getresponse()
        status = json.loads(response.read())
        assert response.status == 200
        assert status["network"]["connected"] is True
        assert {item["ssid"] for item in status["access_points"]} == {"Guest", "Home"}
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_legacy_flat_config_is_migrated_to_full_core_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"station_id":"8531833 - Navesink - Red Bank, NJ"}\n', encoding="utf-8"
    )
    store = AtomicJSONConfigStore(path)

    public = store.update_public({"chart_hours": 24})

    assert public["station_id"] == "8531833"
    loaded = load_config(path)
    assert loaded.station.name == "Navesink - Red Bank, NJ"
    assert loaded.chart_hours == 24


def test_portal_network_port_runs_bounded_setup_and_restores_wifi(tmp_path: Path) -> None:
    events: list[object] = []

    class RuntimeNetwork:
        interface = "wlan0"

        def prepare_client(self, *, timeout_seconds: float) -> RuntimeNetworkStatus:
            events.append(("client", timeout_seconds))
            return RuntimeNetworkStatus(True, "wlan0")

        def start_hotspot(self, ssid: str, *, address: str) -> object:
            events.append(("hotspot", ssid, address))
            return "session"

        def stop_hotspot(self, session: object, *, restore: bool = True) -> None:
            events.append(("stop", session, restore))

    class BoundedServer:
        def __init__(self, application: PortalApplication, address: tuple[str, int]) -> None:
            del application
            events.append(("server", address))

        def serve_for(self, timeout_seconds: float) -> None:
            events.append(("serve", timeout_seconds))

        def close(self) -> None:
            events.append("close")

    application = make_app(tmp_path, FakeNetwork())
    port = PortalNetworkPort(
        RuntimeNetwork(),  # type: ignore[arg-type]
        application,
        server_factory=BoundedServer,
    )

    assert port.prepare(CycleMode.RUN, timeout_seconds=12) == RuntimeNetworkStatus(True, "wlan0")
    assert port.prepare(CycleMode.SETUP, timeout_seconds=30) == RuntimeNetworkStatus(False, "wlan0")
    assert events == [
        ("client", 12),
        ("hotspot", "TideTracker-Setup", "192.168.42.1/24"),
        ("server", ("192.168.42.1", 80)),
        ("serve", 30),
        "close",
        ("stop", "session", True),
    ]


def test_successful_portal_save_ends_session_without_restoring_old_wifi(tmp_path: Path) -> None:
    events: list[object] = []
    application = make_app(tmp_path, FakeNetwork())

    class RuntimeNetwork:
        interface = "wlan0"

        def prepare_client(self, *, timeout_seconds: float) -> RuntimeNetworkStatus:
            del timeout_seconds
            return RuntimeNetworkStatus(True, "wlan0")

        def start_hotspot(self, ssid: str, *, address: str) -> object:
            del ssid, address
            return "session"

        def stop_hotspot(self, session: object, *, restore: bool = True) -> None:
            events.append(("stop", session, restore))

    class SavingServer:
        def __init__(self, app: PortalApplication, address: tuple[str, int]) -> None:
            del address
            self.application = app

        def serve_for(self, timeout_seconds: float) -> None:
            del timeout_seconds
            response = self.application.handle(
                "POST",
                "/api/network/force-save",
                b'{"ssid":"Home","security":"wpa-personal","password":"correct-horse"}',
            )
            assert response.after_send is not None
            response.after_send()

        def close(self) -> None:
            pass

    port = PortalNetworkPort(
        RuntimeNetwork(),  # type: ignore[arg-type]
        application,
        server_factory=SavingServer,
    )

    port.prepare(CycleMode.SETUP, timeout_seconds=30)

    assert events == [("stop", "session", False)]
