from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tidetracker.config import config_to_mapping, default_config
from tidetracker.legacy_oracle import (
    REQUIRED_ASSETS,
    LegacyFixtureError,
    _legacy_noaa_predictions,
    main,
    verify_manifest,
)
from tidetracker.models import RenderModel, SolarTimes, TidePrediction
from tidetracker.release import DeterministicBundleBuilder, assemble_project_payload
from tidetracker.render import PillowRenderer

REPOSITORY = Path(__file__).parents[1]
EMPTY_MANIFEST = Path(__file__).parent / "fixtures" / "legacy_bundles" / "manifest.json"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model() -> RenderModel:
    zone = ZoneInfo("America/New_York")
    generated = datetime(2026, 8, 25, 8, 0, tzinfo=zone)
    prediction_start = datetime(2026, 8, 24, tzinfo=UTC)
    predictions = tuple(
        TidePrediction(
            prediction_start + timedelta(minutes=5 * index),
            math.sin(index / 48),
        )
        for index in range(769)
    )
    solar = SolarTimes(
        local_date=generated.date(),
        timezone=str(zone),
        sunrise=generated.replace(hour=6),
        sunset=generated.replace(hour=19),
    )
    return RenderModel(generated, default_config(), predictions, solar, solar_days=(solar,))


def _solar(value: SolarTimes) -> dict[str, object]:
    return {
        "local_date": value.local_date.isoformat(),
        "timezone": value.timezone,
        "sunrise": value.sunrise.isoformat() if value.sunrise else None,
        "sunset": value.sunset.isoformat() if value.sunset else None,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


class AcceptSignature:
    def verify(self, manifest: Path, signature: Path) -> bool:
        return manifest.is_file() and signature.is_file()


class WriteSignature:
    def sign(self, manifest: Path, signature: Path) -> None:
        assert manifest.is_file()
        signature.write_bytes(b"synthetic signature")


def _approval_files(root: Path) -> tuple[Path, Path]:
    public_key = root / "capture-approver.pub.pem"
    signature = root / "manifest.json.sig"
    public_key.write_text("synthetic public key", encoding="utf-8")
    signature.write_bytes(b"synthetic signature")
    return public_key, signature


def _candidate_bundle(root: Path) -> tuple[Path, str, Path]:
    source = root / "candidate-source"
    entrypoint = assemble_project_payload(REPOSITORY, source)
    bundle = root / "candidate-v2.tar"
    built = DeterministicBundleBuilder(WriteSignature()).build(
        source,
        bundle,
        release_id="candidate-v2",
        entrypoint=entrypoint.relative_to(source).as_posix(),
    )
    public_key = root / "release.pub.pem"
    public_key.write_text("synthetic release public key", encoding="utf-8")
    return bundle, built.sha256, public_key


def test_empty_capture_manifest_fails_closed(tmp_path: Path) -> None:
    report = verify_manifest(EMPTY_MANIFEST)

    assert report["passed"] is False
    assert report["status"] == "capture_required"
    assert report["fixture_count"] == 0

    output = tmp_path / "report.json"
    assert main(
        [
            str(EMPTY_MANIFEST),
            "--output",
            str(output),
            "--signature",
            str(tmp_path / "unused.sig"),
            "--approval-public-key",
            str(tmp_path / "unused.pub"),
            "--candidate-release-id",
            "built-in",
            "--candidate-bundle-sha256",
            "0" * 64,
            "--candidate-bundle",
            str(tmp_path / "unused.tar"),
            "--release-public-key",
            str(tmp_path / "unused-release.pub"),
        ]
    ) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "capture_required"


def test_legacy_noaa_replay_requires_station_local_lst_ldt_time() -> None:
    body = json.dumps(
        {
            "predictions": [
                {"t": "2026-08-25 08:00", "v": "1.0"},
                {"t": "2026-08-25 08:05", "v": "1.1"},
            ]
        }
    ).encode()

    predictions = _legacy_noaa_predictions(
        body,
        station_timezone="America/New_York",
        time_basis="lst_ldt",
    )

    assert predictions[0].timestamp == datetime(
        2026, 8, 25, 8, 0, tzinfo=ZoneInfo("America/New_York")
    )
    with pytest.raises(LegacyFixtureError, match="time_basis"):
        _legacy_noaa_predictions(
            body,
            station_timezone="America/New_York",
            time_basis=None,
        )

    ambiguous = json.dumps(
        {
            "predictions": [
                {"t": "2026-11-01 01:30", "v": "1.0"},
                {"t": "2026-11-01 01:35", "v": "1.1"},
            ]
        }
    ).encode()
    with pytest.raises(LegacyFixtureError, match="ambiguous"):
        _legacy_noaa_predictions(
            ambiguous,
            station_timezone="America/New_York",
            time_basis="lst_ldt",
        )


def test_approved_synthetic_bundle_replays_exactly(tmp_path: Path) -> None:
    model = _model()
    renderer = PillowRenderer(asset_dir=REPOSITORY)
    frame = renderer.render(model, timeout_seconds=5)
    (tmp_path / "framebuffer.bin").write_bytes(frame)
    (tmp_path / "raw-noaa.json").write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "t": item.timestamp.astimezone(
                            ZoneInfo(model.config.station.timezone)
                        ).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "v": str(item.value),
                    }
                    for item in model.predictions
                ]
            }
        ),
        encoding="utf-8",
    )
    bundle = {
        "schema_version": 1,
        "approved": True,
        "fixture_id": "synthetic-verifier-mechanics-only",
        "fixed_clock": model.generated_at.isoformat(),
        "legacy_renderer": {
            "source_commit": "synthetic",
            "python_version": "test",
            "renderer_version": "test",
            "source_tree_sha256": "1" * 64,
            "capture_tool_sha256": "2" * 64,
            "board_serial_sha256": "3" * 64,
            "captured_at": model.generated_at.isoformat(),
        },
        "config": config_to_mapping(model.config),
        "raw_noaa_responses": [
            {
                "product": "predictions",
                "time_basis": "lst_ldt",
                "path": "raw-noaa.json",
                "sha256": _hash(tmp_path / "raw-noaa.json"),
            }
        ],
        "assets": {name: _hash(REPOSITORY / name) for name in REQUIRED_ASSETS},
        "normalized_model": {
            "generated_at": model.generated_at.isoformat(),
            "predictions": [
                {"timestamp": item.timestamp.isoformat(), "value": item.value}
                for item in model.predictions
            ],
            "solar": _solar(model.solar),
            "solar_days": [_solar(item) for item in model.solar_days],
            "offline": False,
            "warnings": [],
        },
        "framebuffer": {
            "path": "framebuffer.bin",
            "bytes": len(frame),
            "sha256": _hash(tmp_path / "framebuffer.bin"),
        },
    }
    bundle_path = tmp_path / "bundle.json"
    _write_json(bundle_path, bundle)
    manifest = {
        "schema_version": 1,
        "status": "approved",
        "bundles": [{"path": "bundle.json", "sha256": _hash(bundle_path)}],
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)
    public_key, signature = _approval_files(tmp_path)
    candidate, candidate_hash, release_key = _candidate_bundle(tmp_path)

    report = verify_manifest(
        manifest_path,
        signature_path=signature,
        approval_public_key=public_key,
        signature_verifier=AcceptSignature(),
        candidate_release_id="candidate-v2",
        candidate_bundle_sha256=candidate_hash,
        candidate_bundle=candidate,
        release_public_key=release_key,
        release_verifier=AcceptSignature(),
    )

    assert report["passed"] is True
    assert report["fixture_count"] == report["matched_count"] == 1
    assert report["candidate_release_public_key_sha256"] == hashlib.sha256(
        release_key.read_bytes()
    ).hexdigest()


def test_mismatched_capture_is_rejected(tmp_path: Path) -> None:
    model = _model()
    frame = PillowRenderer(asset_dir=REPOSITORY).render(model, timeout_seconds=5)
    (tmp_path / "framebuffer.bin").write_bytes(bytes(len(frame)))
    (tmp_path / "raw-noaa.json").write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "t": item.timestamp.astimezone(
                            ZoneInfo(model.config.station.timezone)
                        ).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "v": str(item.value),
                    }
                    for item in model.predictions
                ]
            }
        ),
        encoding="utf-8",
    )
    bundle = {
        "schema_version": 1,
        "approved": True,
        "fixture_id": "mismatch",
        "fixed_clock": model.generated_at.isoformat(),
        "legacy_renderer": {
            "source_commit": "synthetic",
            "python_version": "test",
            "renderer_version": "test",
            "source_tree_sha256": "1" * 64,
            "capture_tool_sha256": "2" * 64,
            "board_serial_sha256": "3" * 64,
            "captured_at": model.generated_at.isoformat(),
        },
        "config": config_to_mapping(model.config),
        "raw_noaa_responses": [
            {
                "product": "predictions",
                "time_basis": "lst_ldt",
                "path": "raw-noaa.json",
                "sha256": _hash(tmp_path / "raw-noaa.json"),
            }
        ],
        "assets": {name: _hash(REPOSITORY / name) for name in REQUIRED_ASSETS},
        "normalized_model": {
            "generated_at": model.generated_at.isoformat(),
            "predictions": [
                {"timestamp": item.timestamp.isoformat(), "value": item.value}
                for item in model.predictions
            ],
            "solar": _solar(model.solar),
            "solar_days": [_solar(item) for item in model.solar_days],
        },
        "framebuffer": {
            "path": "framebuffer.bin",
            "bytes": len(frame),
            "sha256": _hash(tmp_path / "framebuffer.bin"),
        },
    }
    bundle_path = tmp_path / "bundle.json"
    _write_json(bundle_path, bundle)
    manifest_path = tmp_path / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "approved",
            "bundles": [{"path": "bundle.json", "sha256": _hash(bundle_path)}],
        },
    )
    public_key, signature = _approval_files(tmp_path)
    candidate, candidate_hash, release_key = _candidate_bundle(tmp_path)

    report = verify_manifest(
        manifest_path,
        signature_path=signature,
        approval_public_key=public_key,
        signature_verifier=AcceptSignature(),
        candidate_release_id="candidate-v2",
        candidate_bundle_sha256=candidate_hash,
        candidate_bundle=candidate,
        release_public_key=release_key,
        release_verifier=AcceptSignature(),
    )

    assert report["passed"] is False
    assert report["status"] == "failed"
    assert report["failures"] == ["mismatch: framebuffer mismatch"]


def test_verifier_rejects_unbounded_timeout() -> None:
    for value in (math.nan, math.inf):
        try:
            verify_manifest(EMPTY_MANIFEST, timeout_seconds=value)
        except ValueError as exc:
            assert "finite" in str(exc)
        else:  # pragma: no cover - assertion spelling stays explicit
            raise AssertionError("unbounded timeout was accepted")
