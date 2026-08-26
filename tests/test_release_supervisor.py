from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from tidetracker.supervisor import (
    HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE,
    HardwareOwnershipError,
    ReleaseSupervisor,
    SubprocessReleaseExecutor,
)
from tidetracker.updater import ReleaseManifest, ReleaseStateError, ReleaseUpdater


class DigestVerifier:
    def verify(self, manifest: Path, signature: Path) -> bool:
        return signature.read_bytes() == hashlib.sha256(manifest.read_bytes()).digest()


def make_release(tmp_path: Path, release_id: str) -> Path:
    bundle = tmp_path / f"bundle-{release_id}"
    (bundle / "app").mkdir(parents=True)
    payload = f"print({release_id!r})\n".encode()
    (bundle / "app" / "main.py").write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "entrypoint": "app/main.py",
        "files": [
            {
                "path": "app/main.py",
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
    return bundle


def upload(updater: ReleaseUpdater, tmp_path: Path, release_id: str) -> None:
    updater.begin(release_id)
    updater.stage(release_id, make_release(tmp_path, release_id))
    updater.verify(release_id)


class MappingExecutor:
    def __init__(self, returncodes: dict[str, int]) -> None:
        self.returncodes = returncodes
        self.calls: list[str] = []

    def execute(self, directory: Path, manifest: ReleaseManifest) -> int:
        assert directory.name == manifest.release_id
        self.calls.append(manifest.release_id)
        return self.returncodes[manifest.release_id]


def test_successful_trial_is_confirmed(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    executor = MappingExecutor({"v1": 0})

    result = ReleaseSupervisor(updater, executor).run(lambda: 99)

    assert result.returncode == 0
    assert result.trial_confirmed is True
    assert updater.status()["trial"] is None
    assert executor.calls == ["v1"]


def test_failed_trial_rolls_back_and_runs_previous_in_same_boot(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    updater.confirm("v1")
    upload(updater, tmp_path, "v2")
    updater.activate("v2")
    executor = MappingExecutor({"v1": 0, "v2": 7})

    result = ReleaseSupervisor(updater, executor).run(lambda: 99)

    assert result.returncode == 0
    assert result.trial_rolled_back is True
    assert [attempt.source for attempt in result.attempts] == ["v2", "v1"]
    assert executor.calls == ["v2", "v1"]
    assert updater.status()["current"] == "v1"


def test_timed_out_trial_runs_previous_release_in_same_boot(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    updater.confirm("v1")
    upload(updater, tmp_path, "v2")
    updater.activate("v2")
    executor = MappingExecutor({"v1": 0, "v2": 124})

    result = ReleaseSupervisor(updater, executor).run(lambda: 99)

    assert result.returncode == 0
    assert result.trial_rolled_back is True
    assert [(attempt.source, attempt.returncode) for attempt in result.attempts] == [
        ("v2", 124),
        ("v1", 0),
    ]


def test_unproven_hardware_release_blocks_every_fallback(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    built_in_calls = 0

    class OwnershipUnknownExecutor:
        def execute(self, directory: Path, manifest: ReleaseManifest) -> int:
            del directory, manifest
            raise HardwareOwnershipError("process group is still present")

    def built_in() -> int:
        nonlocal built_in_calls
        built_in_calls += 1
        return 0

    result = ReleaseSupervisor(updater, OwnershipUnknownExecutor()).run(built_in)

    assert result.returncode == HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE
    assert result.hardware_ownership_released is False
    assert result.trial_rolled_back is False
    assert [(attempt.source, attempt.returncode) for attempt in result.attempts] == [
        ("v1", HARDWARE_OWNERSHIP_UNPROVEN_RETURN_CODE)
    ]
    assert built_in_calls == 0
    assert updater.status()["current"] == "v1"
    assert updater.status()["trial"] == "v1"


def test_failed_first_trial_rolls_back_to_builtin_in_same_boot(tmp_path: Path) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    executor = MappingExecutor({"v1": 5})
    builtin_calls = 0

    def built_in() -> int:
        nonlocal builtin_calls
        builtin_calls += 1
        return 0

    result = ReleaseSupervisor(updater, executor).run(built_in)

    assert result.returncode == 0
    assert result.trial_rolled_back is True
    assert [attempt.source for attempt in result.attempts] == ["v1", "built-in"]
    assert builtin_calls == 1
    assert updater.status()["current"] is None
    assert updater.status()["trial"] is None


def test_failed_rollback_before_state_change_fails_closed_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    updater.confirm("v1")
    upload(updater, tmp_path, "v2")
    updater.activate("v2")
    executor = MappingExecutor({"v2": 7})
    built_in_calls = 0

    def failed_rollback() -> Path | None:
        raise ReleaseStateError("injected rollback failure")

    def built_in() -> int:
        nonlocal built_in_calls
        built_in_calls += 1
        return 0

    monkeypatch.setattr(updater, "rollback", failed_rollback)

    result = ReleaseSupervisor(updater, executor).run(built_in)

    assert result.returncode == 126
    assert result.trial_rolled_back is False
    assert result.hardware_ownership_released is True
    assert [attempt.source for attempt in result.attempts] == ["v2"]
    assert built_in_calls == 0
    assert updater.status()["current"] == "v2"
    assert updater.status()["trial"] == "v2"


def test_rollback_exception_after_durable_state_change_uses_proven_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = ReleaseUpdater(tmp_path / "state", DigestVerifier())
    upload(updater, tmp_path, "v1")
    updater.activate("v1")
    updater.confirm("v1")
    upload(updater, tmp_path, "v2")
    updater.activate("v2")
    executor = MappingExecutor({"v1": 0, "v2": 7})
    real_rollback = updater.rollback

    def failed_after_commit() -> Path | None:
        real_rollback()
        raise ReleaseStateError("injected post-commit cleanup failure")

    monkeypatch.setattr(updater, "rollback", failed_after_commit)

    result = ReleaseSupervisor(updater, executor).run(lambda: 99)

    assert result.returncode == 0
    assert result.trial_rolled_back is True
    assert [attempt.source for attempt in result.attempts] == ["v2", "v1"]
    assert executor.calls == ["v2", "v1"]
    assert updater.status()["current"] == "v1"
    assert updater.status()["trial"] is None


def test_subprocess_executor_passes_nonrecursive_deferred_cycle_contract(tmp_path: Path) -> None:
    release = tmp_path / "v1"
    entrypoint = release / "entry.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("print('entry')\n", encoding="utf-8")
    manifest = ReleaseManifest("v1", (), "entry.py")
    calls: list[tuple[list[str], Path, dict[str, str], float]] = []

    def runner(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), cwd, dict(env), timeout))
        return subprocess.CompletedProcess(args, 0, "", "")

    executor = SubprocessReleaseExecutor(
        runner=runner,
        timeout_seconds=42,
        environment={"PATH": "safe"},
    )

    assert executor.execute(release, manifest) == 0
    command, cwd, environment, timeout = calls[0]
    assert command[-3:] == ["cycle", "--builtin", "--defer-done"]
    assert command[1] == str(entrypoint)
    assert cwd == release
    assert environment["TIDETRACKER_SUPERVISED"] == "1"
    assert environment["TIDETRACKER_RELEASE_ID"] == "v1"
    assert environment["PYTHONPATH"].split(__import__("os").pathsep)[:2] == [
        str(release / "src"),
        str(release),
    ]
    assert timeout == 42


def test_ambiguous_injected_runner_timeout_is_not_treated_as_released(tmp_path: Path) -> None:
    release = tmp_path / "v1"
    entrypoint = release / "entry.py"
    entrypoint.parent.mkdir()
    entrypoint.write_text("print('entry')\n", encoding="utf-8")

    def runner(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        raise subprocess.TimeoutExpired(args, timeout)

    executor = SubprocessReleaseExecutor(runner=runner, timeout_seconds=0.1)

    with pytest.raises(HardwareOwnershipError, match="without process-tree release proof"):
        executor.execute(release, ReleaseManifest("v1", (), "entry.py"))


def test_release_source_precedes_baseline_pythonpath(tmp_path: Path) -> None:
    release = tmp_path / "v1"
    (release / "src").mkdir(parents=True)
    marker = tmp_path / "imported.txt"
    (release / "src" / "tidetracker_build_probe.py").write_text(
        "VALUE = 'release-source'\n", encoding="utf-8"
    )
    (release / "entry.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from tidetracker_build_probe import VALUE\n"
        "Path(os.environ['PROBE_OUTPUT']).write_text(VALUE, encoding='utf-8')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PROBE_OUTPUT"] = str(marker)
    executor = SubprocessReleaseExecutor(environment=environment, timeout_seconds=10)

    code = executor.execute(release, ReleaseManifest("v1", (), "entry.py"))

    assert code == 0
    assert marker.read_text(encoding="utf-8") == "release-source"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group regression")
def test_candidate_timeout_kills_descendant_before_returning(tmp_path: Path) -> None:
    release = tmp_path / "v1"
    release.mkdir()
    spawned = tmp_path / "descendant-spawned"
    escaped = tmp_path / "descendant-survived-timeout"
    entrypoint = release / "entry.sh"
    entrypoint.write_text(
        "#!/bin/sh\n"
        "(\n"
        f"  printf spawned > {shlex.quote(str(spawned))}\n"
        "  sleep 2\n"
        f"  printf escaped > {shlex.quote(str(escaped))}\n"
        ") &\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    executor = SubprocessReleaseExecutor(timeout_seconds=1.0)

    started = time.monotonic()
    code = executor.execute(release, ReleaseManifest("v1", (), "entry.sh"))
    elapsed = time.monotonic() - started

    assert code == 124
    assert elapsed < 5.0
    assert spawned.is_file()
    time.sleep(2.2)
    assert not escaped.exists()


def test_release_assets_override_baseline_asset_paths(tmp_path: Path) -> None:
    release = tmp_path / "v1"
    release.mkdir()
    (release / "entry.py").write_text("print('entry')\n", encoding="utf-8")
    (release / "Ubuntu-Regular.ttf").write_bytes(b"font")
    (release / "stations.csv").write_text("station", encoding="utf-8")
    captured: list[dict[str, str]] = []

    def runner(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        captured.append(dict(env))
        return subprocess.CompletedProcess(args, 0, "", "")

    executor = SubprocessReleaseExecutor(
        runner=runner,
        environment={
            "TIDETRACKER_ASSET_DIR": "/baseline/assets",
            "TIDETRACKER_STATIONS": "/baseline/stations.csv",
        },
    )

    assert executor.execute(release, ReleaseManifest("v1", (), "entry.py")) == 0
    assert captured[0]["TIDETRACKER_ASSET_DIR"] == str(release)
    assert captured[0]["TIDETRACKER_STATIONS"] == str(release / "stations.csv")


def test_systemd_budget_reserves_full_setup_fallback_after_trial_timeout() -> None:
    unit = (
        Path(__file__).parents[1] / "systemd" / "tidetracker-cycle.service"
    ).read_text(encoding="utf-8")
    timeout_line = next(
        line for line in unit.splitlines() if line.startswith("TimeoutStartSec=")
    )
    service_timeout = int(timeout_line.partition("=")[2])

    candidate_timeout = 900 + 30
    fallback_timeout = 900
    persistence_evidence_done_reserve = 90
    assert service_timeout >= (
        candidate_timeout + fallback_timeout + persistence_evidence_done_reserve
    )
