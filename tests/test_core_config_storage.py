from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tidetracker.storage as storage_module
from tidetracker.config import (
    ConfigError,
    config_from_mapping,
    config_to_mapping,
    default_config,
    load_config,
    migrate_legacy_config,
    save_config,
)
from tidetracker.models import StoredCycle, TidePrediction
from tidetracker.storage import (
    AtomicCycleStore,
    AtomicStore,
    StorageError,
    atomic_write_bytes,
    bounded_system_sync,
    prune_regular_files,
)
from tidetracker.telemetry import JsonlTelemetry


def test_config_round_trip_is_versioned_and_contains_no_credentials(tmp_path) -> None:
    path = tmp_path / "config.json"
    expected = default_config()

    save_config(path, expected)

    assert load_config(path) == expected
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert "wifi" not in json.dumps(raw).lower()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("key", ["password", "wifi_secret", "api_token", "private_key"])
def test_config_rejects_secret_fields_at_any_depth(key: str) -> None:
    raw = config_to_mapping(default_config())
    raw["station"][key] = "must-not-be-written"

    with pytest.raises(ConfigError, match="secret field"):
        config_from_mapping(raw)


def test_config_rejects_unknown_schema_and_unknown_fields() -> None:
    raw = config_to_mapping(default_config())
    raw["schema_version"] = 999
    with pytest.raises(ConfigError, match="unsupported config schema_version"):
        config_from_mapping(raw)

    raw = config_to_mapping(default_config())
    raw["typo"] = True
    with pytest.raises(ConfigError, match="unknown config field"):
        config_from_mapping(raw)


@dataclass(frozen=True)
class CatalogStation:
    station_id: str = "8531833"
    city: str = "Navesink - Red Bank"
    state: str = "NJ"
    latitude: float = 40.355
    longitude: float = -74.065
    timezone: str = "America/New_York"


class Catalog:
    def get(self, station_id: str) -> CatalogStation:
        if station_id != "8531833":
            raise ValueError("unknown")
        return CatalogStation()


def test_legacy_config_migration_resolves_station_and_drops_all_wifi_fields() -> None:
    legacy = {
        "station_id": "8531833 - Navesink - Red Bank, NJ",
        "wifi_ssid": "legacy-network",
        "wifi_password": "must-not-survive",
        "wifi_username": "legacy-user",
        "wifi_conn_type": "WPA2",
    }

    migrated = migrate_legacy_config(legacy, Catalog())
    serialized = json.dumps(config_to_mapping(migrated))

    assert migrated.station.station_id == "8531833"
    assert migrated.station.name == "Navesink - Red Bank, NJ"
    assert "wifi" not in serialized.lower()
    assert "must-not-survive" not in serialized


def test_legacy_config_migration_rejects_non_wifi_secrets() -> None:
    with pytest.raises(ConfigError, match="secret field"):
        migrate_legacy_config(
            {"station_id": "8531833 - Navesink", "api_token": "must-not-survive"}, Catalog()
        )


def test_atomic_write_does_not_replace_destination_when_write_fails(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "state.bin"
    destination.write_bytes(b"old")

    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(StorageError, match="atomic write failed"):
        atomic_write_bytes(destination, b"new")

    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_cycle_store_recovers_exact_private_atomic_write_temps(tmp_path) -> None:
    frames = tmp_path / "frames"
    frames.mkdir()
    digest = "a" * 64
    interrupted = (
        tmp_path / ".last_result.json.deadbeef.tmp",
        tmp_path / ".display_state.json.deadbeef.tmp",
        frames / f".{digest}.bin.deadbeef.tmp",
    )
    for path in interrupted:
        path.write_bytes(b"partial")
        if os.name != "nt":
            path.chmod(0o600)

    AtomicCycleStore(tmp_path)

    assert all(not path.exists() for path in interrupted)


@pytest.mark.skipif(os.name == "nt", reason="Linux directory fsync contract")
def test_linux_directory_sync_failure_is_not_reported_as_durable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = storage_module.os.open

    def fail_directory_open(path, flags, mode=0o777):
        if os.fspath(path) == os.fspath(tmp_path):
            raise OSError("injected directory-open failure")
        return real_open(path, flags, mode)

    monkeypatch.setattr(storage_module.os, "open", fail_directory_open)

    with pytest.raises(StorageError, match="directory for durability sync"):
        atomic_write_bytes(tmp_path / "state.bin", b"new")


def test_atomic_store_rejects_path_traversal(tmp_path) -> None:
    store = AtomicStore(tmp_path / "state")

    with pytest.raises(StorageError, match="escapes"):
        store.write_bytes("../outside", b"no")


def test_cycle_store_round_trip_and_checksum_detection(tmp_path) -> None:
    store = AtomicCycleStore(tmp_path)
    prediction = TidePrediction(datetime(2026, 8, 25, tzinfo=UTC), 1.25)
    framebuffer = bytes(range(256))
    digest = hashlib.sha256(framebuffer).hexdigest()
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        predictions=(prediction,),
        framebuffer=framebuffer,
        framebuffer_sha256=digest,
    )

    store.persist(snapshot)
    assert store.load_last_good() == snapshot

    (tmp_path / "frames" / f"{digest}.bin").write_bytes(b"corrupt")
    with pytest.raises(StorageError, match="checksum"):
        store.load_last_good()


def test_cycle_store_retains_only_current_and_previous_framebuffers(tmp_path) -> None:
    store = AtomicCycleStore(tmp_path)
    prediction = TidePrediction(datetime(2026, 8, 25, tzinfo=UTC), 1.25)
    snapshots: list[StoredCycle] = []
    for index in range(5):
        framebuffer = bytes([index]) * 48_000
        snapshot = StoredCycle(
            station_id="8531833",
            generated_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
            predictions=(prediction,),
            framebuffer=framebuffer,
            framebuffer_sha256=hashlib.sha256(framebuffer).hexdigest(),
        )
        snapshots.append(snapshot)
        store.persist(snapshot)

    retained = {path.stem for path in (tmp_path / "frames").glob("*.bin")}
    assert retained == {
        snapshots[-1].framebuffer_sha256,
        snapshots[-2].framebuffer_sha256,
    }
    assert store.load_last_good() == snapshots[-1]


def test_display_state_requires_explicit_invalidate_then_confirm(tmp_path) -> None:
    store = AtomicCycleStore(tmp_path)
    digest = hashlib.sha256(b"frame").hexdigest()

    assert store.load_display_state() is None
    store.mark_display_state(digest)
    assert store.load_display_state() == digest
    store.invalidate_display_state()
    assert store.load_display_state() is None


def test_loading_last_good_prunes_power_cut_orphans(tmp_path) -> None:
    store = AtomicCycleStore(tmp_path)
    framebuffer = bytes([0xAA]) * 48_000
    snapshot = StoredCycle(
        station_id="8531833",
        generated_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        predictions=(TidePrediction(datetime(2026, 8, 25, tzinfo=UTC), 1.25),),
        framebuffer=framebuffer,
        framebuffer_sha256=hashlib.sha256(framebuffer).hexdigest(),
    )
    store.persist(snapshot)
    for value in (b"orphan-one", b"orphan-two", b"orphan-three"):
        digest = hashlib.sha256(value).hexdigest()
        atomic_write_bytes(tmp_path / "frames" / f"{digest}.bin", value)

    assert store.load_last_good() == snapshot
    assert len(tuple((tmp_path / "frames").glob("*.bin"))) == 2


def test_telemetry_writes_json_lines_and_redacts_secrets(tmp_path) -> None:
    path = tmp_path / "telemetry.jsonl"
    fixed = datetime(2026, 8, 25, 12, tzinfo=UTC)
    telemetry = JsonlTelemetry(path, now=lambda: fixed)

    telemetry.emit(
        "network_test",
        station_id="8531833",
        password="do-not-log",
        nested={"credential": "also-secret", "status": "ok"},
        error_message="connection failed with psk=hunter2",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record == {
        "event": "network_test",
        "error_message": "connection failed with psk=[REDACTED]",
        "nested": {"credential": "[REDACTED]", "status": "ok"},
        "password": "[REDACTED]",
        "station_id": "8531833",
        "timestamp": "2026-08-25T12:00:00+00:00",
    }
    assert "do-not-log" not in path.read_text(encoding="utf-8")


def test_telemetry_rotation_bounds_total_files_and_each_file_size(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    telemetry = JsonlTelemetry(path, max_bytes=160, backup_count=2)

    for index in range(10):
        telemetry.emit("bounded", sequence=index, value="x" * 20)

    retained = tuple(tmp_path.glob("events.jsonl*"))
    assert 2 <= len(retained) <= 3
    assert all(item.stat().st_size <= 160 for item in retained)


def test_telemetry_append_has_a_hard_caller_deadline(tmp_path, monkeypatch) -> None:
    import threading

    path = tmp_path / "events.jsonl"
    telemetry = JsonlTelemetry(path, io_timeout_seconds=0.02)
    real_open = os.open
    release = threading.Event()

    def blocking_open(target, flags, mode=0o777):
        if os.fspath(target) == os.fspath(path):
            release.wait()
        return real_open(target, flags, mode)

    monkeypatch.setattr(os, "open", blocking_open)
    try:
        with pytest.raises(TimeoutError, match="wall-clock deadline"):
            telemetry.emit("blocked")
    finally:
        release.set()


def test_regular_file_retention_keeps_newest_bounded_set(tmp_path) -> None:
    for index in range(5):
        path = tmp_path / f"metric-{index}.json"
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, ns=(index + 1, index + 1))

    prune_regular_files(tmp_path, "*.json", keep=2)

    assert {path.name for path in tmp_path.glob("*.json")} == {
        "metric-3.json",
        "metric-4.json",
    }


def test_regular_file_retention_recovers_prior_per_run_json_temp(tmp_path: Path) -> None:
    interrupted = tmp_path / ".prior-run.json.deadbeef.tmp"
    interrupted.write_bytes(b'{"partial":true')
    if os.name != "nt":
        interrupted.chmod(0o600)
    (tmp_path / "current-run.json").write_text("{}", encoding="utf-8")

    prune_regular_files(tmp_path, "*.json", keep=2)

    assert not interrupted.exists()
    assert (tmp_path / "current-run.json").is_file()


def test_regular_file_retention_fails_closed_on_malformed_owned_temp(tmp_path: Path) -> None:
    (tmp_path / ".prior-run.json.bad!.tmp").write_bytes(b"partial")

    with pytest.raises(StorageError, match="temp is malformed"):
        prune_regular_files(tmp_path, "*.json", keep=2)


def test_system_sync_has_a_hard_caller_deadline() -> None:
    import threading

    release = threading.Event()
    try:
        with pytest.raises(StorageError, match="wall-clock timeout"):
            bounded_system_sync(0.02, sync=release.wait)
    finally:
        release.set()
