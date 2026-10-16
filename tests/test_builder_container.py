from __future__ import annotations

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONTAINER = ROOT / "deploy" / "container"


def test_dockerfile_has_no_external_frontend_and_pins_amd64_base() -> None:
    dockerfile = (CONTAINER / "Dockerfile").read_text(encoding="utf-8")
    assert "# syntax=" not in dockerfile
    assert "COPY --chmod" not in dockerfile
    base = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert re.fullmatch(
        r"FROM --platform=linux/amd64 "
        r"debian:bookworm-20250811-slim@sha256:[0-9a-f]{64}",
        base,
    )


def test_docker_context_is_a_strict_two_input_allowlist() -> None:
    rules = [
        line
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert rules == [
        "**",
        "!.dockerignore",
        "!deploy/",
        "deploy/*",
        "!deploy/container/",
        "deploy/container/*",
        "!deploy/container/Dockerfile",
        "!deploy/container/preflight-sparse-image.sh",
    ]


def test_builder_provenance_files_are_application_hash_inputs() -> None:
    namespace = runpy.run_path(str(ROOT / "deploy" / "hash-application.py"))
    fixed_files = set(namespace["FIXED_FILES"])
    assert {
        ".dockerignore",
        "deploy/container/Dockerfile",
        "deploy/container/preflight-sparse-image.sh",
        "deploy/prepare-keystore.ps1",
    } <= fixed_files


def test_preflight_can_materialize_only_kernel_published_loop_partition_nodes() -> None:
    preflight = (CONTAINER / "preflight-sparse-image.sh").read_text(encoding="utf-8")
    assert "mknod" in preflight
    assert 'partition_sysfs="/sys/class/block/loop${loop_number}p${partition_number}"' in preflight
    assert 'cat "$partition_sysfs/partition"' in preflight
    assert 'cat "$partition_sysfs/dev"' in preflight
    assert 'created_partition_nodes="$created_partition_nodes $partition_node"' in preflight
    assert 'rm -f -- "$partition_node"' in preflight


def _docker_run_commands(readme: str) -> list[str]:
    lines = readme.splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("docker run "):
            index += 1
            continue
        command = [lines[index]]
        while command[-1].rstrip().endswith("`"):
            index += 1
            command.append(lines[index])
        commands.append("\n".join(command))
        index += 1
    return commands


def test_release_containers_use_captured_image_id_and_network_policy() -> None:
    readme = (CONTAINER / "README.md").read_text(encoding="utf-8")
    assert "$BuilderImageId = docker image inspect $BuilderImageTag" in readme
    commands = _docker_run_commands(readme)
    assert len(commands) == 5
    assert all("--privileged" in command for command in commands)
    assert all("$BuilderImageId" in command for command in commands)
    assert all("tidetracker-image-builder:" not in command for command in commands)
    assert sum("--network bridge" in command for command in commands) == 1
    assert sum("--network none" in command for command in commands) == 4
    networked = next(command for command in commands if "--network bridge" in command)
    assert "prepare-package-bundle.sh" in networked
    actual_build = next(command for command in commands if "BUILD-$TIDETRACKER_ROLE" in command)
    assert "--network none" in actual_build
    assert not any(re.search(r"--mount[^\n]*(?:source=|src=)D:", command, re.I) for command in commands)
