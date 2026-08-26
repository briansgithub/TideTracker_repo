from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tidetracker.hil_update import OBSERVED_STATES, HilUpdateError, UpdateCutController
from tidetracker.release import DeterministicBundleBuilder, extract_bundle
from tidetracker.telemetry import UartPhaseTelemetry
from tidetracker.updater import ReleaseUpdater


class DigestSigner:
    def sign(self, manifest: Path, signature: Path) -> None:
        signature.write_bytes(b"test-signature:" + hashlib.sha256(manifest.read_bytes()).digest())


class DigestVerifier:
    def verify(self, manifest: Path, signature: Path) -> bool:
        return signature.read_bytes() == (
            b"test-signature:" + hashlib.sha256(manifest.read_bytes()).digest()
        )


class MemoryWriter:
    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write_line(self, payload: bytes) -> None:
        self.lines.append(payload)

    def close(self) -> None:
        return


class SimulatedPowerCut(RuntimeError):
    pass


class SimulatedRecoveryCut(RuntimeError):
    pass


@dataclass
class PreparedCut:
    root: Path
    public_key: Path
    release_id: str
    bundle: Path
    bundle_sha256: str
    updater: ReleaseUpdater
    controller: UpdateCutController
    writer: MemoryWriter


def simulate_power_cut() -> None:
    raise SimulatedPowerCut


def build_bundle(
    root: Path,
    release_id: str,
    *,
    output_directory: Path | None = None,
) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    entrypoint = source / "main.py"
    entrypoint.write_text("print('candidate')\n", encoding="utf-8")
    output_root = output_directory or root
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / "candidate.tar"
    DeterministicBundleBuilder(DigestSigner()).build(
        source, output, release_id=release_id, entrypoint="main.py"
    )
    return output


def prepare_cut(
    tmp_path: Path,
    phase: str,
    *,
    recovery_checkpoint: Callable[[str], None] | None = None,
) -> PreparedCut:
    release_id = f"candidate-{phase}"
    root = tmp_path / "state"
    bundle = build_bundle(
        tmp_path,
        release_id,
        output_directory=root / "uploads",
    )
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    updater = ReleaseUpdater(root, DigestVerifier(), clock=lambda: 1234.0)
    writer = MemoryWriter()
    public_key = tmp_path / "unused-public-key"
    controller = UpdateCutController(
        root,
        public_key,
        UartPhaseTelemetry(writer),
        sync=lambda: None,
        updater=updater,
        recovery_checkpoint=recovery_checkpoint,
    )
    with pytest.raises(SimulatedPowerCut):
        controller.prepare(
            bundle,
            phase,
            release_id,
            bundle_sha256,
            wait_for_power_cut=simulate_power_cut,
        )
    return PreparedCut(
        root,
        public_key,
        release_id,
        bundle,
        bundle_sha256,
        updater,
        controller,
        writer,
    )


def retry_controller(prepared: PreparedCut) -> UpdateCutController:
    return UpdateCutController(
        prepared.root,
        prepared.public_key,
        UartPhaseTelemetry(MemoryWriter()),
        sync=lambda: None,
        updater=ReleaseUpdater(prepared.root, DigestVerifier(), clock=lambda: 1234.0),
    )


def assert_recovery_clean(prepared: PreparedCut, result: dict[str, object]) -> None:
    status = ReleaseUpdater(
        prepared.root, DigestVerifier(), clock=lambda: 1234.0
    ).status()
    phase = result.get("phase")
    verified = status.get("verified")
    assert isinstance(phase, str)
    assert isinstance(verified, list)
    assert result["observed_state"] == OBSERVED_STATES[phase]
    assert result["state_valid"] is True
    assert result["post_cleanup_clean"] is True
    assert not prepared.bundle.exists()
    assert status["current"] is None
    assert prepared.release_id not in verified


@pytest.mark.parametrize("phase", ["begin", "stage", "verify", "activate"])
def test_update_cut_boundaries_recover_to_clean_state(tmp_path: Path, phase: str) -> None:
    prepared = prepare_cut(tmp_path, phase)
    boundary = json.loads(prepared.writer.lines[-1])
    assert boundary == {
        "schema_version": 1,
        "event": "update_phase_boundary",
        "phase": phase,
        "release_id": prepared.release_id,
        "bundle_sha256": prepared.bundle_sha256,
    }
    result = prepared.controller.recover(
        phase, prepared.release_id, prepared.bundle_sha256
    )
    assert_recovery_clean(prepared, result)
    assert (
        retry_controller(prepared).recover(
            phase, prepared.release_id, prepared.bundle_sha256
        )
        == result
    )


@pytest.mark.parametrize(
    "checkpoint",
    [
        "cleanup_pending",
        "updater_clean",
        "upload_unlinked",
        "upload_removed",
        "upload_clean",
        "history_file_written",
        "history_written",
        "context_unlinked",
    ],
)
def test_second_cut_at_each_recovery_journal_boundary_is_retryable(
    tmp_path: Path, checkpoint: str
) -> None:
    tripped = False

    def cut_once(stage: str) -> None:
        nonlocal tripped
        if stage == checkpoint and not tripped:
            tripped = True
            raise SimulatedRecoveryCut(stage)

    prepared = prepare_cut(tmp_path, "activate", recovery_checkpoint=cut_once)
    with pytest.raises(SimulatedRecoveryCut, match=checkpoint):
        prepared.controller.recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )
    assert tripped is True

    result = retry_controller(prepared).recover(
        "activate", prepared.release_id, prepared.bundle_sha256
    )
    assert_recovery_clean(prepared, result)
    assert (
        retry_controller(prepared).recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )
        == result
    )


@pytest.mark.parametrize("phase", ["begin", "stage"])
def test_retry_repairs_cut_after_incoming_deletion_before_state_clear(
    tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_cut(tmp_path, phase)
    original_abort = prepared.updater.abort

    def cut_after_incoming_delete(release_id: str | None = None) -> str:
        assert release_id == prepared.release_id
        incoming = prepared.updater.incoming / prepared.release_id
        shutil.rmtree(incoming)
        raise SimulatedRecoveryCut("incoming deleted before state clear")

    monkeypatch.setattr(prepared.updater, "abort", cut_after_incoming_delete)
    with pytest.raises(SimulatedRecoveryCut, match="incoming deleted"):
        prepared.controller.recover(phase, prepared.release_id, prepared.bundle_sha256)
    monkeypatch.setattr(prepared.updater, "abort", original_abort)

    result = retry_controller(prepared).recover(
        phase, prepared.release_id, prepared.bundle_sha256
    )
    assert_recovery_clean(prepared, result)


def test_retry_repairs_cut_after_verified_state_clear_before_directory_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_cut(tmp_path, "verify")
    original_discard = prepared.updater.discard_verified

    def cut_after_verified_state_clear(release_id: str) -> str:
        assert release_id == prepared.release_id
        state = prepared.updater._read_state()
        order = prepared.updater._verified_order(state)
        state["verified_order"] = [item for item in order if item != release_id]
        prepared.updater._write_state(state)
        raise SimulatedRecoveryCut("verified state cleared before directory delete")

    monkeypatch.setattr(
        prepared.updater, "discard_verified", cut_after_verified_state_clear
    )
    with pytest.raises(SimulatedRecoveryCut, match="verified state cleared"):
        prepared.controller.recover(
            "verify", prepared.release_id, prepared.bundle_sha256
        )
    monkeypatch.setattr(prepared.updater, "discard_verified", original_discard)

    result = retry_controller(prepared).recover(
        "verify", prepared.release_id, prepared.bundle_sha256
    )
    assert_recovery_clean(prepared, result)


def test_retry_repairs_cut_after_rollback_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare_cut(tmp_path, "activate")
    original_rollback = prepared.updater.rollback

    def cut_after_rollback() -> Path | None:
        restored = original_rollback()
        raise SimulatedRecoveryCut(f"rollback changed state: {restored}")

    monkeypatch.setattr(prepared.updater, "rollback", cut_after_rollback)
    with pytest.raises(SimulatedRecoveryCut, match="rollback changed state"):
        prepared.controller.recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )
    monkeypatch.setattr(prepared.updater, "rollback", original_rollback)

    result = retry_controller(prepared).recover(
        "activate", prepared.release_id, prepared.bundle_sha256
    )
    assert_recovery_clean(prepared, result)


def test_retry_after_rollback_restores_exact_previous_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    updater = ReleaseUpdater(root, DigestVerifier(), clock=lambda: 1234.0)
    previous_build = tmp_path / "previous-build"
    previous_bundle = build_bundle(previous_build, "previous-release")
    previous_extracted = tmp_path / "previous-extracted"
    extract_bundle(previous_bundle, previous_extracted)
    updater.begin("previous-release")
    updater.stage("previous-release", previous_extracted)
    updater.verify("previous-release")
    updater.activate("previous-release")
    updater.confirm("previous-release")

    candidate_build = tmp_path / "candidate-build"
    bundle = build_bundle(
        candidate_build,
        "candidate-activate",
        output_directory=root / "uploads",
    )
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    writer = MemoryWriter()
    controller = UpdateCutController(
        root,
        tmp_path / "unused-public-key",
        UartPhaseTelemetry(writer),
        sync=lambda: None,
        updater=updater,
    )
    with pytest.raises(SimulatedPowerCut):
        controller.prepare(
            bundle,
            "activate",
            "candidate-activate",
            bundle_sha256,
            wait_for_power_cut=simulate_power_cut,
        )

    original_rollback = updater.rollback

    def cut_after_rollback() -> Path | None:
        restored = original_rollback()
        assert restored == root / "releases" / "previous-release"
        raise SimulatedRecoveryCut("rollback restored previous release")

    monkeypatch.setattr(updater, "rollback", cut_after_rollback)
    with pytest.raises(SimulatedRecoveryCut, match="restored previous"):
        controller.recover("activate", "candidate-activate", bundle_sha256)

    retried = UpdateCutController(
        root,
        tmp_path / "unused-public-key",
        UartPhaseTelemetry(MemoryWriter()),
        sync=lambda: None,
        updater=ReleaseUpdater(root, DigestVerifier(), clock=lambda: 1234.0),
    )
    result = retried.recover("activate", "candidate-activate", bundle_sha256)
    status = retried.updater.status()
    verified = status["verified"]
    assert isinstance(verified, list)
    assert result["cleanup_complete"] is True
    assert status["current"] == "previous-release"
    assert status["previous"] is None
    assert status["trial"] is None
    assert verified == ["previous-release"]
    assert (root / "releases" / "previous-release").is_dir()
    assert not (root / "releases" / "candidate-activate").exists()
    assert not bundle.exists()


def test_retry_rejects_request_that_does_not_match_journal_identity(
    tmp_path: Path,
) -> None:
    def cut_after_journal(stage: str) -> None:
        if stage == "cleanup_pending":
            raise SimulatedRecoveryCut(stage)

    prepared = prepare_cut(
        tmp_path,
        "activate",
        recovery_checkpoint=cut_after_journal,
    )
    with pytest.raises(SimulatedRecoveryCut, match="cleanup_pending"):
        prepared.controller.recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )

    wrong_digest = "f" * 64
    assert wrong_digest != prepared.bundle_sha256
    with pytest.raises(HilUpdateError, match="does not match durable context"):
        retry_controller(prepared).recover(
            "activate", prepared.release_id, wrong_digest
        )


def test_retry_never_weakens_bundle_identity_or_accepts_conflicting_history(
    tmp_path: Path
) -> None:
    def cut_after_history_write(stage: str) -> None:
        if stage == "history_file_written":
            raise SimulatedRecoveryCut(stage)

    prepared = prepare_cut(
        tmp_path,
        "activate",
        recovery_checkpoint=cut_after_history_write,
    )
    with pytest.raises(SimulatedRecoveryCut):
        prepared.controller.recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )
    history = prepared.controller._history_path(
        "activate", prepared.release_id, prepared.bundle_sha256
    )
    value = json.loads(history.read_text(encoding="utf-8"))
    value["bundle_sha256"] = "f" * 64
    history.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(HilUpdateError, match="history conflicts"):
        retry_controller(prepared).recover(
            "activate", prepared.release_id, prepared.bundle_sha256
        )


def test_discard_verified_is_idempotent_after_cleanup(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    bundle = build_bundle(tmp_path, "candidate")
    extracted = tmp_path / "extracted"
    extract_bundle(bundle, extracted)
    updater.begin("candidate")
    updater.stage("candidate", extracted)
    updater.verify("candidate")

    assert updater.discard_verified("candidate") == "candidate"
    assert updater.discard_verified("candidate") == "candidate"
    assert updater.status()["verified"] == []
