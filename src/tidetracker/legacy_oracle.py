"""Fail-closed replay verification for legacy framebuffer capture bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import config_from_mapping
from .display import FRAME_BYTES
from .models import RenderModel, SolarTimes, TidePrediction
from .noaa import NoaaResponseError, PredictionValidationError, validate_predictions
from .release import MAX_ARCHIVE_BYTES, BundleError, extract_bundle, resolve_openssl
from .render import PillowRenderer
from .storage import atomic_write_json
from .updater import (
    OpenSSLEd25519Verifier,
    ReleaseUpdater,
    ReleaseVerificationError,
    validate_release_id,
)

SCHEMA_VERSION = 1
REQUIRED_ASSETS = (
    "Ubuntu-Bold.ttf",
    "Ubuntu-Regular.ttf",
    "no_wifi.bmp",
    "sun_rise.png",
)


class LegacyFixtureError(ValueError):
    """Raised when legacy capture evidence is malformed or untrusted."""


class Renderer(Protocol):
    def render(self, model: RenderModel, *, timeout_seconds: float) -> bytes: ...


class SignatureVerifier(Protocol):
    def verify(self, manifest: Path, signature: Path) -> bool: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular_bytes(path: Path, name: str, *, maximum: int = 64 * 1024 * 1024) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise LegacyFixtureError(f"{name} must be a regular non-symlink file")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise LegacyFixtureError(f"{name} must be a regular file")
            value = stream.read(maximum + 1)
    except OSError as exc:
        raise LegacyFixtureError(f"cannot read {name}: {path}") from exc
    if len(value) > maximum:
        raise LegacyFixtureError(f"{name} exceeds its size bound")
    return value


def _json_bytes(value: bytes, name: str) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise LegacyFixtureError(f"{name} contains duplicate JSON key {key!r}")
            result[key] = item
        return result

    try:
        parsed = json.loads(value.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyFixtureError(f"{name} is not valid UTF-8 JSON") from exc
    return _mapping(parsed, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise LegacyFixtureError(f"{name} must be an object with string keys")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise LegacyFixtureError(f"{name} must be an array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise LegacyFixtureError(f"{name} must be a non-empty string")
    return value


def _hash_text(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise LegacyFixtureError(f"{name} must be a lowercase SHA-256")
    return result


def _aware_datetime(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise LegacyFixtureError(f"{name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyFixtureError(f"{name} must include a UTC offset")
    return parsed


def _optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _aware_datetime(value, name)


def _safe_relative(root: Path, value: object, name: str) -> Path:
    raw = Path(_text(value, name))
    if raw.is_absolute() or ".." in raw.parts:
        raise LegacyFixtureError(f"{name} must stay within the fixture directory")
    candidate = (root / raw).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise LegacyFixtureError(f"{name} escapes the fixture directory")
    return candidate


def _load_json(path: Path, name: str) -> Mapping[str, Any]:
    return _json_bytes(_read_regular_bytes(path, name, maximum=2 * 1024 * 1024), name)


def _require_hash(path: Path, expected: object, name: str) -> None:
    expected_text = _hash_text(expected, f"{name}.sha256")
    if not path.is_file() or _sha256(path) != expected_text:
        raise LegacyFixtureError(f"{name} is missing or has the wrong SHA-256")


def _require_bytes_hash(value: bytes, expected: object, name: str) -> None:
    if _sha256_bytes(value) != _hash_text(expected, f"{name}.sha256"):
        raise LegacyFixtureError(f"{name} has the wrong SHA-256")


def _solar(value: object, name: str) -> SolarTimes:
    raw = _mapping(value, name)
    try:
        local_date = date.fromisoformat(_text(raw.get("local_date"), f"{name}.local_date"))
    except ValueError as exc:
        raise LegacyFixtureError(f"{name}.local_date must be an ISO date") from exc
    return SolarTimes(
        local_date=local_date,
        timezone=_text(raw.get("timezone"), f"{name}.timezone"),
        sunrise=_optional_datetime(raw.get("sunrise"), f"{name}.sunrise"),
        sunset=_optional_datetime(raw.get("sunset"), f"{name}.sunset"),
    )


def _legacy_noaa_predictions(
    body: bytes,
    *,
    station_timezone: str,
    time_basis: object,
    max_predictions: int = 4096,
) -> tuple[TidePrediction, ...]:
    """Parse the legacy NOAA `lst_ldt` response as station-local civil time."""

    if time_basis != "lst_ldt":
        raise LegacyFixtureError(
            "raw NOAA response time_basis must be the legacy value 'lst_ldt'"
        )
    try:
        zone = ZoneInfo(station_timezone)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - config normally catches this
        raise LegacyFixtureError("legacy station timezone is unavailable") from exc
    payload = _json_bytes(body, "raw NOAA response")
    if "error" in payload:
        raise NoaaResponseError("NOAA response contains an error")
    entries = _sequence(payload.get("predictions"), "raw NOAA predictions")
    if len(entries) > max_predictions:
        raise PredictionValidationError("NOAA response contains too many predictions")

    predictions: list[TidePrediction] = []
    for index, item in enumerate(entries):
        raw = _mapping(item, f"raw NOAA prediction {index}")
        try:
            naive = datetime.strptime(
                _text(raw.get("t"), f"raw NOAA prediction {index}.t"),
                "%Y-%m-%d %H:%M",
            )
            value = float(raw["v"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NoaaResponseError(f"prediction {index} is malformed") from exc

        fold_zero = naive.replace(tzinfo=zone, fold=0)
        fold_one = naive.replace(tzinfo=zone, fold=1)
        roundtrip_zero = fold_zero.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        roundtrip_one = fold_one.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        zero_valid = roundtrip_zero == naive
        one_valid = roundtrip_one == naive
        if not zero_valid and not one_valid:
            raise LegacyFixtureError(
                f"raw NOAA prediction {index} uses a nonexistent station-local time"
            )
        if zero_valid and one_valid and fold_zero.utcoffset() != fold_one.utcoffset():
            raise LegacyFixtureError(
                f"raw NOAA prediction {index} uses an ambiguous station-local time"
            )
        predictions.append(TidePrediction(timestamp=fold_zero, value=value))
    return validate_predictions(tuple(predictions), max_predictions=max_predictions)


def _render_model(bundle: Mapping[str, Any]) -> RenderModel:
    normalized = _mapping(bundle.get("normalized_model"), "normalized_model")
    config = config_from_mapping(_mapping(bundle.get("config"), "config"))
    generated_at = _aware_datetime(normalized.get("generated_at"), "generated_at")
    fixed_clock = _aware_datetime(bundle.get("fixed_clock"), "fixed_clock")
    if generated_at != fixed_clock:
        raise LegacyFixtureError("fixed_clock must equal normalized_model.generated_at")
    predictions: list[TidePrediction] = []
    for index, item in enumerate(_sequence(normalized.get("predictions"), "predictions")):
        prediction = _mapping(item, f"predictions[{index}]")
        value = prediction.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LegacyFixtureError(f"predictions[{index}].value must be numeric")
        predictions.append(
            TidePrediction(
                timestamp=_aware_datetime(
                    prediction.get("timestamp"), f"predictions[{index}].timestamp"
                ),
                value=float(value),
            )
        )
    if len(predictions) < 2:
        raise LegacyFixtureError("normalized_model must contain at least two predictions")
    solar_days = tuple(
        _solar(item, f"solar_days[{index}]")
        for index, item in enumerate(_sequence(normalized.get("solar_days", []), "solar_days"))
    )
    warnings_raw = _sequence(normalized.get("warnings", []), "warnings")
    if not all(isinstance(item, str) for item in warnings_raw):
        raise LegacyFixtureError("warnings must contain only strings")
    offline = normalized.get("offline", False)
    if not isinstance(offline, bool):
        raise LegacyFixtureError("offline must be boolean")
    return RenderModel(
        generated_at=generated_at,
        config=config,
        predictions=tuple(predictions),
        solar=_solar(normalized.get("solar"), "solar"),
        solar_days=solar_days,
        offline=offline,
        warnings=tuple(cast(Sequence[str], warnings_raw)),
    )


def render_capture_bundle(
    bundle_path: Path,
    *,
    asset_root: Path,
    output: Path,
    timeout_seconds: float,
) -> None:
    """Internal subprocess entry: render one normalized capture using this release."""

    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180:
        raise LegacyFixtureError("render timeout must be finite and at most 180 seconds")
    bundle = _load_json(bundle_path, "legacy bundle")
    framebuffer = PillowRenderer(asset_dir=asset_root).render(
        _render_model(bundle), timeout_seconds=timeout_seconds
    )
    if len(framebuffer) != FRAME_BYTES:
        raise LegacyFixtureError(f"candidate renderer returned {len(framebuffer)} bytes")
    with output.open("xb") as stream:
        stream.write(framebuffer)
        stream.flush()
        os.fsync(stream.fileno())


def _candidate_render(
    candidate_root: Path,
    bundle_path: Path,
    *,
    output: Path,
    timeout_seconds: float,
) -> bytes:
    script = (
        "import pathlib,sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from tidetracker.legacy_oracle import render_capture_bundle;"
        "render_capture_bundle(pathlib.Path(sys.argv[2]),asset_root=pathlib.Path(sys.argv[3]),"
        "output=pathlib.Path(sys.argv[4]),timeout_seconds=float(sys.argv[5]))"
    )
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TZ", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(candidate_root / "src"),
                str(bundle_path),
                str(candidate_root),
                str(output),
                str(timeout_seconds),
            ],
            cwd=candidate_root,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=min(185.0, timeout_seconds + 5.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LegacyFixtureError("candidate renderer subprocess failed") from exc
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.split())[:300]
        raise LegacyFixtureError(
            f"candidate renderer subprocess returned {completed.returncode}: {detail}"
        )
    return _read_regular_bytes(output, "candidate framebuffer", maximum=FRAME_BYTES)


def _prepare_candidate(
    candidate_bundle: Path,
    *,
    candidate_bundle_sha256: str,
    candidate_release_id: str,
    release_public_key: Path,
    release_verifier: SignatureVerifier | None,
    openssl_binary: str | None,
    workspace: Path,
) -> tuple[Path, str, str]:
    archive_bytes = _read_regular_bytes(
        candidate_bundle, "candidate release bundle", maximum=MAX_ARCHIVE_BYTES
    )
    if _sha256_bytes(archive_bytes) != candidate_bundle_sha256:
        raise LegacyFixtureError("candidate release bundle SHA-256 does not match")
    private_archive = workspace / "candidate-release.tar"
    private_archive.write_bytes(archive_bytes)
    candidate_root = workspace / "candidate"
    try:
        extract_bundle(private_archive, candidate_root)
    except BundleError as exc:
        raise LegacyFixtureError("candidate release bundle cannot be extracted safely") from exc

    release_key_bytes = _read_regular_bytes(
        release_public_key, "release public key", maximum=64 * 1024
    )
    private_key = workspace / "release-approver.pub.pem"
    private_key.write_bytes(release_key_bytes)
    verifier = release_verifier or OpenSSLEd25519Verifier(
        private_key, binary=openssl_binary or "openssl"
    )
    updater = ReleaseUpdater(workspace / "release-verification", verifier)
    try:
        updater._verify_directory(candidate_release_id, candidate_root)
    except ReleaseVerificationError as exc:
        raise LegacyFixtureError("candidate release signature or payload is invalid") from exc
    required = candidate_root / "src" / "tidetracker" / "legacy_oracle.py"
    if not required.is_file():
        raise LegacyFixtureError("candidate release lacks the legacy replay implementation")
    return (
        candidate_root,
        _sha256(candidate_root / "manifest.json"),
        _sha256_bytes(release_key_bytes),
    )


def _verify_bundle(
    bundle_path: Path,
    *,
    bundle_bytes: bytes,
    candidate_root: Path,
    snapshot_root: Path,
    seen_capture_paths: set[Path],
    seen_capture_hashes: set[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    bundle = _json_bytes(bundle_bytes, "legacy bundle")
    if bundle.get("schema_version") != SCHEMA_VERSION or bundle.get("approved") is not True:
        raise LegacyFixtureError("legacy bundle is not an approved schema-1 capture")
    fixture_id = _text(bundle.get("fixture_id"), "fixture_id")
    provenance = _mapping(bundle.get("legacy_renderer"), "legacy_renderer")
    _text(provenance.get("source_commit"), "legacy_renderer.source_commit")
    _text(provenance.get("python_version"), "legacy_renderer.python_version")
    _text(provenance.get("renderer_version"), "legacy_renderer.renderer_version")
    _hash_text(provenance.get("source_tree_sha256"), "legacy_renderer.source_tree_sha256")
    _hash_text(provenance.get("capture_tool_sha256"), "legacy_renderer.capture_tool_sha256")
    _hash_text(provenance.get("board_serial_sha256"), "legacy_renderer.board_serial_sha256")
    _aware_datetime(provenance.get("captured_at"), "legacy_renderer.captured_at")
    model = _render_model(bundle)

    raw_responses = _sequence(bundle.get("raw_noaa_responses"), "raw_noaa_responses")
    if not raw_responses:
        raise LegacyFixtureError("at least one raw NOAA response is required")
    raw_predictions: list[TidePrediction] = []
    private_bundle = snapshot_root / "bundle.json"
    private_bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_bundle.write_bytes(bundle_bytes)
    for index, item in enumerate(raw_responses):
        raw = _mapping(item, f"raw_noaa_responses[{index}]")
        if raw.get("product") != "predictions":
            raise LegacyFixtureError(f"raw_noaa_responses[{index}].product is unsupported")
        path = _safe_relative(bundle_path.parent, raw.get("path"), f"raw response {index}")
        if path in seen_capture_paths:
            raise LegacyFixtureError("capture manifest reuses a raw/framebuffer path")
        seen_capture_paths.add(path)
        raw_bytes = _read_regular_bytes(path, f"raw response {index}")
        _require_bytes_hash(raw_bytes, raw.get("sha256"), f"raw response {index}")
        raw_hash = _sha256_bytes(raw_bytes)
        if raw_hash in seen_capture_hashes:
            raise LegacyFixtureError("capture manifest reuses raw/framebuffer content")
        seen_capture_hashes.add(raw_hash)
        try:
            raw_predictions.extend(
                _legacy_noaa_predictions(
                    raw_bytes,
                    station_timezone=model.config.station.timezone,
                    time_basis=raw.get("time_basis"),
                )
            )
        except (NoaaResponseError, PredictionValidationError) as exc:
            raise LegacyFixtureError(f"raw response {index} is not valid NOAA predictions") from exc

    assets = _mapping(bundle.get("assets"), "assets")
    for name in REQUIRED_ASSETS:
        _require_hash(candidate_root / name, assets.get(name), f"asset {name}")

    framebuffer = _mapping(bundle.get("framebuffer"), "framebuffer")
    expected_path = _safe_relative(
        bundle_path.parent, framebuffer.get("path"), "framebuffer.path"
    )
    if framebuffer.get("bytes") != FRAME_BYTES:
        raise LegacyFixtureError(f"framebuffer.bytes must equal {FRAME_BYTES}")
    if expected_path in seen_capture_paths:
        raise LegacyFixtureError("capture manifest reuses a raw/framebuffer path")
    seen_capture_paths.add(expected_path)
    expected = _read_regular_bytes(expected_path, "framebuffer", maximum=FRAME_BYTES)
    _require_bytes_hash(expected, framebuffer.get("sha256"), "framebuffer")
    expected_hash = _sha256_bytes(expected)
    if expected_hash in seen_capture_hashes:
        raise LegacyFixtureError("capture manifest reuses raw/framebuffer content")
    seen_capture_hashes.add(expected_hash)
    if len(expected) != FRAME_BYTES:
        raise LegacyFixtureError(f"captured framebuffer must contain {FRAME_BYTES} bytes")

    if tuple(raw_predictions) != model.predictions:
        raise LegacyFixtureError(
            "normalized predictions do not match the captured raw NOAA responses"
        )
    output = snapshot_root / "candidate-framebuffer.bin"
    actual = _candidate_render(
        candidate_root,
        private_bundle,
        output=output,
        timeout_seconds=timeout_seconds,
    )
    actual_hash = hashlib.sha256(actual).hexdigest()
    return {
        "fixture_id": fixture_id,
        "matched": len(actual) == FRAME_BYTES and actual == expected,
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "actual_bytes": len(actual),
    }


def verify_manifest(
    manifest_path: Path,
    *,
    timeout_seconds: float = 15.0,
    signature_path: Path | None = None,
    approval_public_key: Path | None = None,
    signature_verifier: SignatureVerifier | None = None,
    candidate_release_id: str = "built-in",
    candidate_bundle_sha256: str = "0" * 64,
    candidate_bundle: Path | None = None,
    release_public_key: Path | None = None,
    release_verifier: SignatureVerifier | None = None,
    openssl_binary: str | None = None,
) -> dict[str, Any]:
    """Replay every approved capture and return a machine-readable gate report."""

    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180:
        raise ValueError("timeout_seconds must be finite and at most 180")
    try:
        candidate_release_id = validate_release_id(candidate_release_id)
    except ValueError as exc:
        raise LegacyFixtureError("candidate_release_id is invalid") from exc
    if (
        len(candidate_bundle_sha256) != 64
        or any(char not in "0123456789abcdef" for char in candidate_bundle_sha256)
    ):
        raise LegacyFixtureError("candidate_bundle_sha256 must be a lowercase SHA-256")
    if candidate_release_id != "built-in" and candidate_bundle_sha256 == "0" * 64:
        raise LegacyFixtureError("verified candidate requires its nonzero bundle SHA-256")
    manifest_bytes = _read_regular_bytes(
        manifest_path, "legacy manifest", maximum=2 * 1024 * 1024
    )
    manifest = _json_bytes(manifest_bytes, "legacy manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise LegacyFixtureError("legacy manifest must use schema_version 1")
    entries = _sequence(manifest.get("bundles"), "bundles")
    if entries and manifest.get("status") != "approved":
        raise LegacyFixtureError("non-empty legacy manifest must have approved status")
    signature_hash: str | None = None
    public_key_hash: str | None = None
    candidate_manifest_hash: str | None = None
    candidate_release_public_key_hash: str | None = None
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    if entries:
        if (
            signature_path is None
            or approval_public_key is None
            or candidate_bundle is None
            or release_public_key is None
        ):
            raise LegacyFixtureError("approved legacy manifest requires signature and public key")
        with tempfile.TemporaryDirectory(prefix="tidetracker-legacy-verify-") as raw_temp:
            workspace = Path(raw_temp)
            private_manifest = workspace / "capture-manifest.json"
            private_manifest.write_bytes(manifest_bytes)
            signature_bytes = _read_regular_bytes(
                signature_path, "capture manifest signature", maximum=1024
            )
            key_bytes = _read_regular_bytes(
                approval_public_key, "capture approval public key", maximum=64 * 1024
            )
            private_signature = workspace / "capture-manifest.json.sig"
            private_signature.write_bytes(signature_bytes)
            private_approval_key = workspace / "capture-approver.pub.pem"
            private_approval_key.write_bytes(key_bytes)
            verifier = signature_verifier or OpenSSLEd25519Verifier(
                private_approval_key, binary=openssl_binary or "openssl"
            )
            try:
                verified = verifier.verify(private_manifest, private_signature)
            except Exception as exc:
                raise LegacyFixtureError("legacy manifest signature verification failed") from exc
            if not verified:
                raise LegacyFixtureError("legacy manifest signature is invalid")
            signature_hash = _sha256_bytes(signature_bytes)
            public_key_hash = _sha256_bytes(key_bytes)
            (
                candidate_root,
                candidate_manifest_hash,
                candidate_release_public_key_hash,
            ) = _prepare_candidate(
                candidate_bundle,
                candidate_bundle_sha256=candidate_bundle_sha256,
                candidate_release_id=candidate_release_id,
                release_public_key=release_public_key,
                release_verifier=release_verifier,
                openssl_binary=openssl_binary,
                workspace=workspace,
            )

            seen_paths: set[Path] = set()
            seen_bundle_hashes: set[str] = set()
            seen_capture_paths: set[Path] = set()
            seen_capture_hashes: set[str] = set()
            seen_fixture_ids: set[str] = set()
            for index, item in enumerate(entries):
                entry = _mapping(item, f"bundles[{index}]")
                try:
                    path = _safe_relative(
                        manifest_path.parent,
                        entry.get("path"),
                        f"bundles[{index}].path",
                    )
                    if path in seen_paths:
                        raise LegacyFixtureError(
                            "legacy manifest contains a duplicate bundle path"
                        )
                    seen_paths.add(path)
                    bundle_bytes = _read_regular_bytes(
                        path, f"bundles[{index}]", maximum=2 * 1024 * 1024
                    )
                    _require_bytes_hash(bundle_bytes, entry.get("sha256"), f"bundles[{index}]")
                    bundle_hash = _sha256_bytes(bundle_bytes)
                    if bundle_hash in seen_bundle_hashes:
                        raise LegacyFixtureError(
                            "legacy manifest contains duplicate bundle content"
                        )
                    seen_bundle_hashes.add(bundle_hash)
                    result = _verify_bundle(
                        path,
                        bundle_bytes=bundle_bytes,
                        candidate_root=candidate_root,
                        snapshot_root=workspace / "captures" / str(index),
                        seen_capture_paths=seen_capture_paths,
                        seen_capture_hashes=seen_capture_hashes,
                        timeout_seconds=timeout_seconds,
                    )
                    results.append(result)
                    fixture_id = cast(str, result["fixture_id"])
                    if fixture_id in seen_fixture_ids:
                        failures.append(f"{fixture_id}: duplicate fixture ID")
                    seen_fixture_ids.add(fixture_id)
                    if not result["matched"]:
                        failures.append(f"{result['fixture_id']}: framebuffer mismatch")
                except (LegacyFixtureError, OSError, ValueError) as exc:
                    failures.append(f"bundle {index}: {exc}")
    fixture_count = len(entries)
    matched_count = sum(result["matched"] is True for result in results)
    passed = fixture_count > 0 and not failures and matched_count == fixture_count
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "legacy_framebuffer_verification",
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "capture_manifest_signature_sha256": signature_hash,
        "capture_approval_public_key_sha256": public_key_hash,
        "candidate_release_id": candidate_release_id,
        "candidate_bundle_sha256": candidate_bundle_sha256,
        "candidate_manifest_sha256": candidate_manifest_hash,
        "candidate_release_public_key_sha256": candidate_release_public_key_hash,
        "candidate_verification": (
            "signed_bundle_subprocess" if entries else "capture_required"
        ),
        "fixture_count": fixture_count,
        "matched_count": matched_count,
        "passed": passed,
        "status": "passed" if passed else ("capture_required" if fixture_count == 0 else "failed"),
        "results": results,
        "failures": failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--approval-public-key", type=Path, required=True)
    parser.add_argument("--candidate-release-id", required=True)
    parser.add_argument("--candidate-bundle-sha256", required=True)
    parser.add_argument("--candidate-bundle", type=Path, required=True)
    parser.add_argument("--release-public-key", type=Path, required=True)
    parser.add_argument(
        "--openssl",
        default="openssl" if os.name == "nt" else "/usr/bin/openssl",
    )
    args = parser.parse_args(argv)
    try:
        openssl = Path(resolve_openssl(args.openssl)).resolve()
        if not openssl.is_absolute() or not openssl.is_file():
            raise LegacyFixtureError("OpenSSL must resolve to an absolute existing binary")
        report = verify_manifest(
            args.manifest,
            timeout_seconds=args.timeout,
            signature_path=args.signature,
            approval_public_key=args.approval_public_key,
            candidate_release_id=args.candidate_release_id,
            candidate_bundle_sha256=args.candidate_bundle_sha256,
            candidate_bundle=args.candidate_bundle,
            release_public_key=args.release_public_key,
            openssl_binary=str(openssl),
        )
    except (LegacyFixtureError, OSError, ValueError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "event": "legacy_framebuffer_verification",
            "fixture_count": 0,
            "matched_count": 0,
            "passed": False,
            "status": "invalid",
            "results": [],
            "failures": [str(exc)],
        }
    atomic_write_json(args.output, report)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
