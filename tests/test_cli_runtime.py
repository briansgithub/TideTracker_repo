from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from tidetracker import cli
from tidetracker.config import default_config
from tidetracker.models import CycleMode
from tidetracker.release import DeterministicBundleBuilder, extract_bundle
from tidetracker.updater import ReleaseUpdater


class DigestSigner:
    def sign(self, manifest: Path, signature: Path) -> None:
        signature.write_bytes(hashlib.sha256(manifest.read_bytes()).digest())


class DigestVerifier:
    def verify(self, manifest: Path, signature: Path) -> bool:
        return signature.read_bytes() == hashlib.sha256(manifest.read_bytes()).digest()


class InvalidSigner:
    def sign(self, manifest: Path, signature: Path) -> None:
        del manifest
        signature.write_bytes(b"invalid")


def test_release_trial_timeout_is_mode_aware_for_field_power_safety() -> None:
    config = default_config()

    assert cli._release_trial_timeout(config, CycleMode.RUN) == 210
    assert cli._release_trial_timeout(config, CycleMode.SETUP) == 930


def test_simulated_cycle_writes_frame_state_and_actual_metrics(tmp_path: Path) -> None:
    code = cli.main(["cycle", "--simulate", "--data-dir", str(tmp_path)])

    assert code == 0
    assert (tmp_path / "results" / "simulated-framebuffer.bin").stat().st_size == 48_000
    metrics_files = list((tmp_path / "telemetry").glob("*.json"))
    assert len(metrics_files) == 1
    metrics = json.loads(metrics_files[0].read_text(encoding="utf-8"))
    assert metrics["event"] == "cycle_metrics"
    assert metrics["success"] is True
    assert metrics["wake_to_done_seconds"] >= 0
    assert metrics["max_rss_kib"] >= 0
    assert metrics["display_refresh_count"] == 1
    assert metrics["wifi_active_seconds"] >= 0
    assert metrics["release_id"] == "built-in"
    assert metrics["execution_source"] == "built-in"
    assert metrics["production_route_interface"] == "wlan0"
    assert metrics["wifi_active_observed_seconds"] == 0
    assert metrics["wifi_active_observation_source"] == "simulated"
    result_path = tmp_path / "state" / "cycle" / "last_result.json"
    assert result_path.is_file()
    durable = json.loads(result_path.read_text(encoding="utf-8"))
    assert durable["release_id"] == "built-in"
    assert durable["production_route_interface"] == "wlan0"
    supervision = json.loads(
        (tmp_path / "state" / "supervisor" / "last_execution.json").read_text(
            encoding="utf-8"
        )
    )
    execution = supervision["execution"]
    assert execution["expected_release_executed_successfully"] is True
    assert execution["fallback_used"] is False
    assert execution["selected_run_id"] == durable["run_id"]


def test_service_exit_distinguishes_handled_cycle_failure(tmp_path: Path) -> None:
    manual = cli.main(
        [
            "cycle",
            "--simulate",
            "--simulate-offline",
            "--data-dir",
            str(tmp_path / "manual"),
        ]
    )
    service = cli.main(
        [
            "cycle",
            "--simulate",
            "--simulate-offline",
            "--service",
            "--data-dir",
            str(tmp_path / "service"),
        ]
    )

    assert manual == 1
    assert service == 0
    result = json.loads(
        (tmp_path / "service" / "state" / "cycle" / "last_result.json").read_text(
            encoding="utf-8"
        )
    )
    assert result["success"] is False
    assert result["error_type"] == "OfflineDataUnavailable"


def test_active_release_cycle_inherits_simulation_and_data_paths(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text(
        "from tidetracker.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    updater = ReleaseUpdater(tmp_path, DigestVerifier())
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    payload = (source / "entry.py").read_bytes()
    (bundle / "entry.py").write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "release_id": "candidate",
        "entrypoint": "entry.py",
        "files": [
            {
                "path": "entry.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (bundle / "manifest.sig").write_bytes(hashlib.sha256(manifest_path.read_bytes()).digest())
    updater.begin("candidate")
    updater.stage("candidate", bundle)
    updater.verify("candidate")
    updater.activate("candidate")
    monkeypatch.setattr(cli, "OpenSSLEd25519Verifier", lambda _path: DigestVerifier())
    monkeypatch.setenv("PYTHONPATH", str(Path(cli.__file__).resolve().parents[1]))

    code = cli.main(["cycle", "--simulate", "--data-dir", str(tmp_path)])

    assert code == 0
    assert updater.status()["trial"] is None
    assert (tmp_path / "results" / "simulated-framebuffer.bin").stat().st_size == 48_000


def test_exit_zero_candidate_without_fresh_matching_result_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "OpenSSLEd25519Verifier", lambda _path: DigestVerifier())
    assert cli.main(["cycle", "--simulate", "--data-dir", str(tmp_path)]) == 0
    prior = json.loads(
        (tmp_path / "state" / "cycle" / "last_result.json").read_text(encoding="utf-8")
    )

    bundle = tmp_path / "noop-bundle"
    bundle.mkdir()
    payload = b"raise SystemExit(0)\n"
    (bundle / "noop.py").write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "release_id": "noop-candidate",
        "entrypoint": "noop.py",
        "files": [
            {
                "path": "noop.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (bundle / "manifest.sig").write_bytes(hashlib.sha256(manifest_path.read_bytes()).digest())
    updater = ReleaseUpdater(tmp_path, DigestVerifier())
    updater.begin("noop-candidate")
    updater.stage("noop-candidate", bundle)
    updater.verify("noop-candidate")
    updater.activate("noop-candidate")

    assert cli.main(["cycle", "--simulate", "--data-dir", str(tmp_path)]) == 0

    durable = json.loads(
        (tmp_path / "state" / "cycle" / "last_result.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (tmp_path / "state" / "supervisor" / "last_execution.json").read_text(
            encoding="utf-8"
        )
    )["execution"]
    assert durable["run_id"] != prior["run_id"]
    assert durable["release_id"] == "built-in"
    first_attempt = evidence["attempts"][0]
    assert first_attempt["evidence_valid"] is False
    assert first_attempt["execution_source"] == "verified-release"
    assert first_attempt["release_id"] == "noop-candidate"
    assert first_attempt["returncode"] == 0
    assert first_attempt["run_id"] is None
    assert evidence["fallback_used"] is True
    assert evidence["trial_rolled_back"] is True
    assert evidence["selected_release_id"] == "built-in"
    assert evidence["selected_success"] is True
    assert evidence["expected_release_executed_successfully"] is False
    assert updater.status()["current"] is None


def test_deferred_done_cannot_be_selected_outside_supervisor(tmp_path: Path) -> None:
    code = cli.main(
        [
            "cycle",
            "--simulate",
            "--builtin",
            "--defer-done",
            "--data-dir",
            str(tmp_path),
        ]
    )

    assert code == 2


def test_stage_bundle_convenience_verifies_and_activates_trial(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    entrypoint = source / "entry.py"
    entrypoint.write_text("print('release')\n", encoding="utf-8")
    bundle = tmp_path / "release.tar"
    DeterministicBundleBuilder(DigestSigner()).build(
        source,
        bundle,
        release_id="v1",
        entrypoint="entry.py",
    )
    monkeypatch.setattr(cli, "OpenSSLEd25519Verifier", lambda _path: DigestVerifier())
    root = tmp_path / "target"

    code = cli.update_main(["--root", str(root), "stage", "--bundle", str(bundle)])

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "trial"
    updater = ReleaseUpdater(root, DigestVerifier())
    assert updater.status()["current"] == "v1"
    assert updater.status()["trial"] == "v1"


def test_build_bundle_command_never_packages_or_reports_private_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("print('release')\n", encoding="utf-8")
    private_key = tmp_path / "signing-private.pem"
    private_key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    output = tmp_path / "candidate.tar"
    monkeypatch.setattr(cli, "OpenSSLEd25519Signer", lambda _path: DigestSigner())

    code = cli.update_main(
        [
            "build-bundle",
            "--release-id",
            "candidate",
            "--source",
            str(source),
            "--entrypoint",
            "entry.py",
            "--private-key",
            str(private_key),
            "--output",
            str(output),
        ]
    )

    assert code == 0
    rendered = capsys.readouterr().out
    assert str(private_key) not in rendered
    assert "PRIVATE KEY MATERIAL" not in output.read_bytes().decode("latin-1")
    assert extract_bundle(output, tmp_path / "extracted").release_id == "candidate"


def test_build_bundle_rejects_private_key_inside_payload(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("print('release')\n", encoding="utf-8")
    private_key = source / "private.pem"
    private_key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")

    code = cli.update_main(
        [
            "build-bundle",
            "--release-id",
            "candidate",
            "--source",
            str(source),
            "--entrypoint",
            "entry.py",
            "--private-key",
            str(private_key),
            "--output",
            str(tmp_path / "candidate.tar"),
        ]
    )

    assert code == 2
    assert "outside the payload source" in capsys.readouterr().err


def test_project_bundle_command_assembles_curated_pure_python_release(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project_root = Path(cli.__file__).resolve().parents[2]
    private_key = tmp_path / "signing-private.pem"
    private_key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    output = tmp_path / "project-release.tar"
    monkeypatch.setattr(cli, "OpenSSLEd25519Signer", lambda _path: DigestSigner())

    code = cli.update_main(
        [
            "build-bundle",
            "--release-id",
            "project-v1",
            "--project-root",
            str(project_root),
            "--private-key",
            str(private_key),
            "--output",
            str(output),
        ]
    )

    assert code == 0
    capsys.readouterr()
    destination = tmp_path / "project-release"
    manifest = extract_bundle(output, destination)
    assert manifest.entrypoint == "tidetracker-release.py"
    assert (destination / "src" / "tidetracker" / "cli.py").is_file()
    assert (destination / "stations.csv").is_file()
    assert not list(destination.rglob("*.so"))
    assert not (destination / ".git").exists()


def test_stage_bundle_recovers_partial_upload_and_removes_transferred_archive(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("print('release')\n", encoding="utf-8")
    archive = root / "uploads" / "candidate.tar"
    DeterministicBundleBuilder(DigestSigner()).build(
        source,
        archive,
        release_id="candidate",
        entrypoint="entry.py",
    )
    updater = ReleaseUpdater(root, DigestVerifier())
    partial = updater.begin("candidate")
    (partial / "interrupted").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(cli, "OpenSSLEd25519Verifier", lambda _path: DigestVerifier())

    assert cli.update_main(["--root", str(root), "stage", "--bundle", str(archive)]) == 0

    assert not archive.exists()
    assert updater.status()["current"] == "candidate"


def test_failed_stage_removes_transferred_archive_without_stale_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = tmp_path / "target"
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_text("print('release')\n", encoding="utf-8")
    archive = root / "uploads" / "invalid.tar"
    DeterministicBundleBuilder(InvalidSigner()).build(
        source,
        archive,
        release_id="invalid",
        entrypoint="entry.py",
    )
    monkeypatch.setattr(cli, "OpenSSLEd25519Verifier", lambda _path: DigestVerifier())

    assert cli.update_main(["--root", str(root), "stage", "--bundle", str(archive)]) == 2

    capsys.readouterr()
    assert not archive.exists()
    assert ReleaseUpdater(root, DigestVerifier()).status()["in_progress"] is None


def test_diagnose_json_honors_data_dir(tmp_path: Path, capsys) -> None:
    assert cli.main(["diagnose", "--json", "--data-dir", str(tmp_path)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["data_dir"]["path"] == str(tmp_path)


def test_display_oracle_hash_checks_and_uses_file_display_only(
    tmp_path: Path, capsys
) -> None:
    bitmap = tmp_path / "legacy.bmp"
    image = Image.new("1", (800, 480), 1)
    image.putpixel((10, 20), 0)
    image.save(bitmap)
    digest = hashlib.sha256(bitmap.read_bytes()).hexdigest()
    output = tmp_path / "controller.bin"

    assert cli.main(
        [
            "display-oracle",
            "--bitmap",
            str(bitmap),
            "--sha256",
            digest,
            "--display-output",
            str(output),
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["physical"] is False
    assert report["refresh_count"] == 1
    assert output.stat().st_size == 48_000


def test_display_oracle_rejects_unchecked_or_unguarded_physical_input(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    bitmap = tmp_path / "legacy.bmp"
    Image.new("1", (800, 480), 1).save(bitmap)
    digest = hashlib.sha256(bitmap.read_bytes()).hexdigest()

    assert cli.main(
        [
            "display-oracle",
            "--bitmap",
            str(bitmap),
            "--sha256",
            "0" * 64,
        ]
    ) == 2
    assert "mismatch" in capsys.readouterr().err
    assert cli.main(
        [
            "display-oracle",
            "--bitmap",
            str(bitmap),
            "--sha256",
            digest,
            "--physical",
        ]
    ) == 2
    assert "HIL guard" in capsys.readouterr().err

    monkeypatch.setenv("TIDETRACKER_HIL_DISPLAY_ORACLE", "1")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(
        [
            "display-oracle",
            "--bitmap",
            str(bitmap),
            "--sha256",
            digest,
            "--physical",
        ]
    ) == 2
    assert "root HIL helper" in capsys.readouterr().err
