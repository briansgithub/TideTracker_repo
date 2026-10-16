from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from tidetracker.key_policy import KeyPolicyError, build_key_policy, validate_key_policy

ROOT = pathlib.Path(__file__).parents[1]


def openssh_key(fill: int) -> bytes:
    key_type = b"ssh-ed25519"
    raw = bytes([fill]) * 32
    wire = (
        len(key_type).to_bytes(4, "big")
        + key_type
        + len(raw).to_bytes(4, "big")
        + raw
    )
    return b"ssh-ed25519 " + base64.b64encode(wire) + b" test\n"


def pem_key(fill: int) -> bytes:
    der = bytes.fromhex("302a300506032b6570032100") + bytes([fill]) * 32
    return (
        b"-----BEGIN PUBLIC KEY-----\n"
        + base64.b64encode(der)
        + b"\n-----END PUBLIC KEY-----\n"
    )


def five_key_policy() -> dict[str, object]:
    return build_key_policy(
        workstation_to_controller_ssh=openssh_key(1),
        controller_to_zero_ssh=openssh_key(2),
        application_release=pem_key(3),
        lab_safety=pem_key(4),
        image_release=pem_key(5),
    )


def test_hil_example_pins_controller_to_zero_identity_on_tidedata() -> None:
    config = json.loads((ROOT / "lab" / "hil_config.example.json").read_text(encoding="utf-8"))

    assert config["target"]["identity_file"] == (
        "/var/lib/tidetracker/controller/ssh/controller-to-zero-ed25519"
    )
    assert config["target"]["known_hosts_file"] == (
        "/var/lib/tidetracker/controller/ssh/zero-known-hosts"
    )


def test_role_builder_requires_image_key_and_compares_canonical_material() -> None:
    builder = (ROOT / "deploy" / "build-role-image.sh").read_text(encoding="utf-8")
    provision = (ROOT / "deploy" / "provision-profile.sh").read_text(encoding="utf-8")
    firstboot = (ROOT / "deploy" / "libexec" / "tidetracker-firstboot").read_text(
        encoding="utf-8"
    )

    for argument in (
        "--workstation-to-controller-ssh-public-key",
        "--controller-to-zero-ssh-public-key",
        "--application-release-public-key",
        "--lab-safety-public-key",
        "--image-release-public-key",
    ):
        assert argument in builder
    assert "python3 -m tidetracker.key_policy" in builder
    assert '"schema_version": 2' in builder
    assert "key_policy_sha256" in builder
    assert "set -C" in builder
    assert "/etc/tidetracker/image-release-ed25519.pub.pem" in provision
    assert "reconstructed_policy = build_key_policy(**public_keys)" in firstboot


def test_flasher_canonicalizes_openssh_ed25519_material() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    raw_key = bytes(range(32))
    key_type = b"ssh-ed25519"
    wire = (
        len(key_type).to_bytes(4, "big")
        + key_type
        + len(raw_key).to_bytes(4, "big")
        + raw_key
    )
    record = f"ssh-ed25519 {base64.b64encode(wire).decode('ascii')} test"
    expected = hashlib.sha256(bytes.fromhex("302a300506032b6570032100") + raw_key).hexdigest()
    script_path = str(ROOT / "deploy" / "flash-sd.ps1").replace("'", "''")
    encoded_record = base64.b64encode(record.encode("utf-8")).decode("ascii")
    command = f"""
$tokens=$null
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile(
    '{script_path}', [ref]$tokens, [ref]$errors
)
if ($errors.Count) {{ throw 'flash script parse failed' }}
foreach ($name in @('Get-BytesSha256','Get-OpenSshEd25519KeyIdentity')) {{
    $definition=$ast.Find({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }}, $true)
    Invoke-Expression $definition.Extent.Text
}}
$bytes=[Convert]::FromBase64String('{encoded_record}')
$identity=Get-OpenSshEd25519KeyIdentity -Bytes $bytes -Label test
$identity.CanonicalSha256
"""

    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout.strip() == expected


def test_flasher_explicitly_rejects_role_manifest_schema_one() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    script_path = str(ROOT / "deploy" / "flash-sd.ps1").replace("'", "''")
    legacy = base64.b64encode(b'{"schema_version":1}').decode("ascii")
    command = f"""
$tokens=$null
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile(
    '{script_path}', [ref]$tokens, [ref]$errors
)
if ($errors.Count) {{ throw 'flash script parse failed' }}
foreach ($name in @('Assert-NoDuplicateJsonProperties','Test-RoleImageManifest')) {{
    $definition=$ast.Find({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }}, $true)
    Invoke-Expression $definition.Extent.Text
}}
$bytes=[Convert]::FromBase64String('{legacy}')
$observed=''
try {{
    Test-RoleImageManifest -ManifestBytes $bytes -Role zero -ActualImageHash ('a' * 64)
}}
catch {{
    $observed=$_.Exception.Message
}}
[Console]::Out.WriteLine($observed)
if ($observed -notmatch 'schema 1 is explicitly unsupported') {{ exit 9 }}
"""

    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_key_policy_rejects_boolean_schema_and_canonical_reuse() -> None:
    policy = five_key_policy()
    policy["schema_version"] = True
    with pytest.raises(KeyPolicyError, match="schema_version"):
        validate_key_policy(policy)

    with pytest.raises(KeyPolicyError, match="must be distinct"):
        build_key_policy(
            workstation_to_controller_ssh=openssh_key(1),
            controller_to_zero_ssh=openssh_key(1),
            application_release=pem_key(3),
            lab_safety=pem_key(4),
            image_release=pem_key(5),
        )


@pytest.mark.parametrize("collision", ("image", "checksum", "manifest"))
def test_builder_refuses_existing_output_without_partial_publication(
    tmp_path: pathlib.Path,
    collision: str,
) -> None:
    builder = (ROOT / "deploy" / "build-role-image.sh").read_text(encoding="utf-8")
    snippet_start = builder.index(
        "import json\nimport os\nimport pathlib\nimport sys\n\n"
        "from tidetracker.key_policy import POLICY_FIELDS"
    )
    snippet_end = builder.index('\nPY\necho "Built and hashed $output"', snippet_start)
    publisher = builder[snippet_start:snippet_end]
    working = tmp_path / "zero.img.partial"
    output = tmp_path / "zero.img"
    checksum = pathlib.Path(f"{output}.sha256")
    manifest = pathlib.Path(f"{output}.manifest.json")
    policy_path = tmp_path / "key-policy.json"
    working.write_bytes(b"candidate-image")
    colliding_output = {
        "image": output,
        "checksum": checksum,
        "manifest": manifest,
    }[collision]
    colliding_output.write_bytes(b"do-not-replace")
    policy_path.write_text(json.dumps(five_key_policy()), encoding="ascii")
    digest = "a" * 64

    result = subprocess.run(
        [
            sys.executable,
            "-",
            str(working),
            str(output),
            digest,
            "zero",
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "512",
            str(policy_path),
        ],
        input=publisher,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )

    assert result.returncode != 0
    assert colliding_output.read_bytes() == b"do-not-replace"
    assert working.read_bytes() == b"candidate-image"
    for final_output in (output, checksum, manifest):
        if final_output != colliding_output:
            assert not final_output.exists()
    assert not pathlib.Path(f"{output}.sha256.partial").exists()
    assert not pathlib.Path(f"{output}.manifest.json.partial").exists()


def test_builder_loop_nodes_require_validated_sysfs_and_track_cleanup() -> None:
    builder = (ROOT / "deploy" / "build-role-image.sh").read_text(encoding="utf-8")

    assert "mknod" in builder
    assert 'sysfs_partition="/sys/class/block/$partition_name"' in builder
    assert 'cat "$sysfs_partition/partition"' in builder
    assert 'cat "$sysfs_partition/dev"' in builder
    assert 'created_partition_nodes="$created_partition_nodes $partition_node"' in builder
    assert 'rm -f -- "$created_node"' in builder
    assert 'cleanup_safe=true' in builder
