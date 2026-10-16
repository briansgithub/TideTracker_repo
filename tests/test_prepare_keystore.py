from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Iterator

import pytest

from tidetracker.key_policy import (
    build_key_policy,
    openssh_ed25519_der,
    pem_ed25519_der,
)

ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "deploy" / "prepare-keystore.ps1"
CONFIRMATION = "CREATE-TIDETRACKER-KEYSTORE-v1"
KEY_NAMES = {
    "workstation_to_controller_ssh",
    "controller_to_zero_ssh",
    "application_release",
    "lab_safety",
    "image_release",
}
RUNTIME_ROLES = (
    "openssl_executable",
    "openssl_msys_crypto",
    "openssl_msys_ssl",
    "openssl_msys_runtime",
    "ssh_keygen_executable",
    "ssh_keygen_libcrypto",
)
TEST_MODE = "ALLOW-DISPOSABLE-LOCALAPPDATA-OVERRIDE-v1"


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pwsh() -> str:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 is unavailable")
    return executable


def _tool_path(name: str, fallback: pathlib.Path) -> pathlib.Path:
    discovered = shutil.which(name)
    path = pathlib.Path(discovered) if discovered is not None else fallback
    if not path.is_file():
        pytest.skip(f"{name} is unavailable")
    return path.resolve()


def _tools() -> tuple[pathlib.Path, pathlib.Path]:
    openssl = _tool_path(
        "openssl",
        pathlib.Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Git"
        / "usr"
        / "bin"
        / "openssl.exe",
    )
    ssh_keygen = _tool_path(
        "ssh-keygen",
        pathlib.Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        / "System32"
        / "OpenSSH"
        / "ssh-keygen.exe",
    )
    return openssl, ssh_keygen


def _unused_non_d_drive() -> str:
    query_dos_device = ctypes.WinDLL("kernel32", use_last_error=True).QueryDosDeviceW
    query_dos_device.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    query_dos_device.restype = ctypes.c_uint32
    for letter in "ZYXWVUTSRQPONMLKJIHGFE":
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        if query_dos_device(f"{letter}:", buffer, len(buffer)) != 0:
            continue
        if ctypes.get_last_error() == 2:
            return f"{letter}:"
    pytest.skip("No unused non-D DOS drive designator is available")


@contextlib.contextmanager
def _substituted_drive(target: pathlib.Path) -> Iterator[str]:
    resolved_target = target.resolve()
    assert resolved_target.is_dir()
    assert resolved_target.drive.upper() != "D:"
    drive = _unused_non_d_drive()
    subst = pathlib.Path(os.environ["SYSTEMROOT"]) / "System32" / "subst.exe"
    assert subst.is_file()
    create = subprocess.run(
        [str(subst), drive, str(resolved_target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if create.returncode != 0:
        pytest.skip(f"A disposable SUBST drive could not be created: {create.stderr}")
    try:
        yield drive
    finally:
        remove = subprocess.run(
            [str(subst), drive, "/D"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert remove.returncode == 0, remove.stderr


def _environment(local_app_data: pathlib.Path) -> dict[str, str]:
    root = local_app_data.resolve()
    assert root.drive.upper() != "D:"
    try:
        root.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise AssertionError("functional keystore tests may not write inside the repository")
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(root)
    environment["TIDETRACKER_KEYSTORE_TEST_MODE"] = TEST_MODE
    return environment


def _run(
    local_app_data: pathlib.Path,
    *arguments: str,
    timeout: int = 90,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_environment(local_app_data),
    )


def _inspect_runtime(
    local_app_data: pathlib.Path,
    openssl: pathlib.Path, ssh_keygen: pathlib.Path
) -> tuple[str, dict[str, object]]:
    result = _run(
        local_app_data,
        "-OpenSslPath",
        str(openssl),
        "-ExpectedOpenSslSha256",
        _sha256(openssl),
        "-SshKeygenPath",
        str(ssh_keygen),
        "-ExpectedSshKeygenSha256",
        _sha256(ssh_keygen),
    )

    assert result.returncode == 0, result.stderr
    match = re.search(
        r"runtime manifest SHA-256: ([0-9a-f]{64})", result.stdout
    )
    assert match is not None, result.stdout
    json_lines = [
        line for line in result.stdout.splitlines() if line.startswith('{"files":')
    ]
    assert len(json_lines) == 1, result.stdout
    assert "runtime files were inspected read-only" in result.stdout
    assert not local_app_data.exists()
    return match.group(1), json.loads(json_lines[0])


def _creation_arguments(
    openssl: pathlib.Path,
    ssh_keygen: pathlib.Path,
    runtime_manifest_sha256: str,
) -> tuple[str, ...]:
    return (
        "-OpenSslPath",
        str(openssl),
        "-ExpectedOpenSslSha256",
        _sha256(openssl),
        "-SshKeygenPath",
        str(ssh_keygen),
        "-ExpectedSshKeygenSha256",
        _sha256(ssh_keygen),
        "-ExpectedRuntimeManifestSha256",
        runtime_manifest_sha256,
        "-Apply",
        "-Execute",
        "-Confirmation",
        CONFIRMATION,
    )


def _assert_runtime_manifest(
    manifest: dict[str, object],
    openssl: pathlib.Path,
    ssh_keygen: pathlib.Path,
) -> None:
    assert set(manifest) == {"schema_version", "os_dll_policy", "files"}
    assert manifest["schema_version"] == 1
    assert (
        manifest["os_dll_policy"]
        == "Windows protected platform DLLs are the operating-system TCB."
    )
    files = manifest["files"]
    assert isinstance(files, list)
    assert [entry["role"] for entry in files] == list(RUNTIME_ROLES)
    expected_paths = (
        openssl,
        openssl.parent / "msys-crypto-3.dll",
        openssl.parent / "msys-ssl-3.dll",
        openssl.parent / "msys-2.0.dll",
        ssh_keygen,
        ssh_keygen.parent.parent / "libcrypto.dll",
    )
    for entry, expected_path in zip(files, expected_paths, strict=True):
        assert set(entry) == {"path", "role", "sha256", "size"}
        assert pathlib.Path(entry["path"]).resolve() == expected_path.resolve()
        assert entry["sha256"] == _sha256(expected_path)
        assert entry["size"] == expected_path.stat().st_size


def test_prepare_keystore_source_contains_required_safety_boundaries() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "$env:LOCALAPPDATA",
        "TideTracker\\keystore",
        "CREATE-TIDETRACKER-KEYSTORE-v1",
        "Drive D: is reserved",
        "The keystore may not be created inside the repository",
        "FileAttributes]::ReparsePoint",
        "FileMode]::CreateNew",
        "Directory]::Move",
        "ExpectedOpenSslSha256",
        "ExpectedSshKeygenSha256",
        "ExpectedRuntimeManifestSha256",
        "TIDETRACKER_KEYSTORE_TEST_MODE",
        "TIDETRACKER_KEYSTORE_TEST_RESERVED_DRIVE",
        "S-1-5-18",
        "SetAccessRuleProtection($true, $false)",
    ):
        assert required in source
    assert "Remove-Item" not in source
    assert "private_key_sha256" not in source


def test_prepare_keystore_default_is_a_write_free_dry_run(tmp_path: pathlib.Path) -> None:
    local_app_data = tmp_path / "dry-run-local-app-data"

    result = _run(local_app_data)

    assert result.returncode == 0, result.stderr
    assert "DRY RUN: no directory or key was created." in result.stdout
    assert not local_app_data.exists()


@pytest.mark.parametrize(
    "arguments, expected_error",
    [
        (("-Apply",), "requires both -Apply and -Execute"),
        (("-Execute",), "requires both -Apply and -Execute"),
        (("-Confirmation", CONFIRMATION), "requires both -Apply and -Execute"),
        (
            ("-Apply", "-Execute", "-Confirmation", "wrong-token"),
            f"Confirmation must be exactly {CONFIRMATION}",
        ),
    ],
)
def test_prepare_keystore_rejects_incomplete_authorization_without_writing(
    tmp_path: pathlib.Path,
    arguments: tuple[str, ...],
    expected_error: str,
) -> None:
    local_app_data = tmp_path / "incomplete-local-app-data"

    result = _run(local_app_data, *arguments)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not local_app_data.exists()


def test_prepare_keystore_rejects_drive_d_before_filesystem_access() -> None:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = r"D:\tidetracker-string-only-never-accessed"

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode != 0
    assert "Drive D: is reserved" in result.stderr


def test_prepare_keystore_rejects_repository_destination_without_writing() -> None:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(ROOT)
    environment["TIDETRACKER_KEYSTORE_TEST_MODE"] = TEST_MODE

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode != 0
    assert "may not be created inside the repository" in result.stderr
    assert not (ROOT / "TideTracker" / "keystore" / "v1").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows SUBST identity test")
def test_prepare_keystore_rejects_repository_destination_through_subst_alias() -> None:
    with _substituted_drive(ROOT) as drive:
        environment = os.environ.copy()
        environment["LOCALAPPDATA"] = f"{drive}\\"
        environment["TIDETRACKER_KEYSTORE_TEST_MODE"] = TEST_MODE

        result = subprocess.run(
            [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    assert result.returncode != 0
    assert "may not be created inside the repository" in result.stderr
    assert not (ROOT / "TideTracker" / "keystore" / "v1").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved-device alias test")
def test_prepare_keystore_rejects_reserved_subst_target_without_writing(
    tmp_path: pathlib.Path,
) -> None:
    reserved_target = tmp_path / "reserved-target"
    reserved_target.mkdir()
    local_app_data = reserved_target / "candidate-local-app-data"
    with _substituted_drive(reserved_target) as drive:
        environment = _environment(local_app_data)
        environment["TIDETRACKER_KEYSTORE_TEST_RESERVED_DRIVE"] = drive

        result = subprocess.run(
            [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    assert result.returncode != 0
    assert "resolves onto the reserved removable-drive device" in result.stderr
    assert not local_app_data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction rejection test")
def test_prepare_keystore_rejects_reparse_ancestor_without_writing(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "junction-target"
    link = tmp_path / "junction-local-app-data"
    target.mkdir()
    junction_environment = {
        **os.environ,
        "TIDETRACKER_TEST_LINK": str(link),
        "TIDETRACKER_TEST_TARGET": str(target),
    }
    junction = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Junction -Path $env:TIDETRACKER_TEST_LINK "
            "-Target $env:TIDETRACKER_TEST_TARGET | Out-Null",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=junction_environment,
    )
    if junction.returncode != 0:
        pytest.skip("A disposable junction could not be created")
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(link)
    environment["TIDETRACKER_KEYSTORE_TEST_MODE"] = TEST_MODE

    result = subprocess.run(
        [_pwsh(), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode != 0
    assert "reparse" in result.stderr.lower() or "regular directory" in result.stderr.lower()
    assert not (target / "TideTracker" / "keystore" / "v1").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows executable identity test")
def test_prepare_keystore_rejects_tool_digest_mismatch_before_writing(
    tmp_path: pathlib.Path,
) -> None:
    openssl, ssh_keygen = _tools()
    local_app_data = tmp_path / "bad-digest-local-app-data"
    runtime_digest, _manifest = _inspect_runtime(
        local_app_data, openssl, ssh_keygen
    )
    arguments = list(_creation_arguments(openssl, ssh_keygen, runtime_digest))
    digest_index = arguments.index("-ExpectedOpenSslSha256") + 1
    arguments[digest_index] = "0" * 64

    result = _run(local_app_data, *arguments)

    assert result.returncode != 0
    assert "OpenSSL SHA-256 mismatch" in result.stderr
    assert not local_app_data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime identity test")
def test_prepare_keystore_rejects_runtime_manifest_mismatch_before_writing(
    tmp_path: pathlib.Path,
) -> None:
    openssl, ssh_keygen = _tools()
    local_app_data = tmp_path / "bad-runtime-manifest-local-app-data"
    runtime_digest, manifest = _inspect_runtime(
        local_app_data, openssl, ssh_keygen
    )
    _assert_runtime_manifest(manifest, openssl, ssh_keygen)
    wrong_digest = "0" * 64 if runtime_digest != "0" * 64 else "1" * 64
    arguments = _creation_arguments(openssl, ssh_keygen, wrong_digest)

    result = _run(local_app_data, *arguments)

    assert result.returncode != 0
    assert "runtime manifest SHA-256 mismatch" in result.stderr
    assert not local_app_data.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted cleanup test")
def test_prepare_keystore_cleans_only_failed_staging_sibling(
    tmp_path: pathlib.Path,
) -> None:
    openssl, _ssh_keygen = _tools()
    failing_tool = (
        pathlib.Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "OpenSSH"
        / "ssh-add.exe"
    )
    assert failing_tool.is_file()
    local_app_data = tmp_path / "failed-generation-local-app-data"
    runtime_digest, _manifest = _inspect_runtime(
        local_app_data, openssl, failing_tool
    )
    arguments = _creation_arguments(openssl, failing_tool, runtime_digest)

    result = _run(local_app_data, *arguments)

    assert result.returncode != 0
    assert "output was suppressed" in result.stderr
    parent = local_app_data / "TideTracker" / "keystore"
    assert parent.is_dir()
    assert not (parent / "v1").exists()
    assert not list(parent.glob("v1.partial.*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL and OpenSSH integration test")
def test_prepare_keystore_creates_valid_atomic_non_overwriting_v1(
    tmp_path: pathlib.Path,
) -> None:
    openssl, ssh_keygen = _tools()
    local_app_data = tmp_path / "functional-local-app-data"
    runtime_digest, inspected_manifest = _inspect_runtime(
        local_app_data, openssl, ssh_keygen
    )
    _assert_runtime_manifest(inspected_manifest, openssl, ssh_keygen)
    arguments = _creation_arguments(openssl, ssh_keygen, runtime_digest)

    result = _run(local_app_data, *arguments)

    assert result.returncode == 0, result.stderr
    destination = local_app_data / "TideTracker" / "keystore" / "v1"
    assert destination.is_dir()
    assert not list(destination.parent.glob("v1.partial.*"))
    private_files = {path.name for path in (destination / "private").iterdir()}
    assert private_files == {
        "workstation-to-controller-ssh-ed25519",
        "controller-to-zero-ssh-ed25519",
        "application-release-ed25519.private.pem",
        "lab-safety-approval-ed25519.private.pem",
        "image-release-ed25519.private.pem",
    }
    public_paths = {
        "workstation_to_controller_ssh": destination
        / "public"
        / "workstation-to-controller-ssh-ed25519.pub",
        "controller_to_zero_ssh": destination
        / "public"
        / "controller-to-zero-ssh-ed25519.pub",
        "application_release": destination
        / "public"
        / "application-release-ed25519.public.pem",
        "lab_safety": destination
        / "public"
        / "lab-safety-approval-ed25519.public.pem",
        "image_release": destination
        / "public"
        / "image-release-ed25519.public.pem",
    }
    assert all(path.is_file() and path.stat().st_size > 0 for path in public_paths.values())
    expected_tree = {
        "private",
        "public",
        "records",
        "private/workstation-to-controller-ssh-ed25519",
        "private/controller-to-zero-ssh-ed25519",
        "private/application-release-ed25519.private.pem",
        "private/lab-safety-approval-ed25519.private.pem",
        "private/image-release-ed25519.private.pem",
        "public/workstation-to-controller-ssh-ed25519.pub",
        "public/controller-to-zero-ssh-ed25519.pub",
        "public/application-release-ed25519.public.pem",
        "public/lab-safety-approval-ed25519.public.pem",
        "public/image-release-ed25519.public.pem",
        "records/key-policy.json",
        "records/public-keystore-record.json",
    }
    assert {
        path.relative_to(destination).as_posix() for path in destination.rglob("*")
    } == expected_tree

    ssh_private_paths = {
        "workstation_to_controller_ssh": destination
        / "private"
        / "workstation-to-controller-ssh-ed25519",
        "controller_to_zero_ssh": destination
        / "private"
        / "controller-to-zero-ssh-ed25519",
    }
    for name, private_path in ssh_private_paths.items():
        derived = subprocess.run(
            [str(ssh_keygen), "-y", "-f", str(private_path)],
            check=False,
            capture_output=True,
            timeout=30,
        )
        assert derived.returncode == 0, derived.stderr.decode("utf-8", errors="replace")
        assert openssh_ed25519_der(derived.stdout, name) == openssh_ed25519_der(
            public_paths[name].read_bytes(), name
        )

    pem_private_paths = {
        "application_release": destination
        / "private"
        / "application-release-ed25519.private.pem",
        "lab_safety": destination
        / "private"
        / "lab-safety-approval-ed25519.private.pem",
        "image_release": destination
        / "private"
        / "image-release-ed25519.private.pem",
    }
    openssl_environment = {**os.environ, "OPENSSL_CONF": "NUL"}
    for name, private_path in pem_private_paths.items():
        derived = subprocess.run(
            [str(openssl), "pkey", "-in", str(private_path), "-pubout"],
            check=False,
            capture_output=True,
            timeout=30,
            env=openssl_environment,
        )
        assert derived.returncode == 0, derived.stderr.decode("utf-8", errors="replace")
        assert pem_ed25519_der(derived.stdout, name) == pem_ed25519_der(
            public_paths[name].read_bytes(), name
        )

    policy = json.loads((destination / "records" / "key-policy.json").read_text("utf-8"))
    expected_policy = build_key_policy(
        workstation_to_controller_ssh=public_paths[
            "workstation_to_controller_ssh"
        ].read_bytes(),
        controller_to_zero_ssh=public_paths["controller_to_zero_ssh"].read_bytes(),
        application_release=public_paths["application_release"].read_bytes(),
        lab_safety=public_paths["lab_safety"].read_bytes(),
        image_release=public_paths["image_release"].read_bytes(),
    )
    assert policy == expected_policy
    material_hashes = {
        value
        for key, value in policy.items()
        if key.endswith("_key_material_sha256")
    }
    assert len(material_hashes) == 5

    record_path = destination / "records" / "public-keystore-record.json"
    record_text = record_path.read_text("utf-8")
    record = json.loads(record_text)
    assert "private" not in record_text.lower()
    assert set(record) == {
        "schema_version",
        "keystore_version",
        "created_utc",
        "current_user_sid",
        "tools",
        "keys",
        "key_policy",
        "runtime_manifest",
        "runtime_manifest_sha256",
    }
    assert record["schema_version"] == 1
    assert record["keystore_version"] == "v1"
    assert set(record["keys"]) == KEY_NAMES
    assert record["key_policy"] == policy
    assert set(record["tools"]) == {"openssl", "ssh_keygen"}
    for tool_record in record["tools"].values():
        assert set(tool_record) == {"path", "size", "sha256", "file_version"}
    assert record["tools"]["openssl"]["sha256"] == _sha256(openssl)
    assert record["tools"]["ssh_keygen"]["sha256"] == _sha256(ssh_keygen)
    assert record["runtime_manifest"] == inspected_manifest
    assert record["runtime_manifest_sha256"] == runtime_digest
    _assert_runtime_manifest(record["runtime_manifest"], openssl, ssh_keygen)
    canonical_runtime_manifest = json.dumps(
        record["runtime_manifest"], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert hashlib.sha256(canonical_runtime_manifest).hexdigest() == runtime_digest
    for key_record in record["keys"].values():
        assert set(key_record) == {
            "algorithm",
            "format",
            "public_file",
            "public_key_sha256",
            "key_material_sha256",
            "fingerprint_sha256",
        }
        assert re.fullmatch(r"[0-9a-f]{64}", key_record["public_key_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", key_record["key_material_sha256"])
        assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", key_record["fingerprint_sha256"])
        assert key_record["public_file"].startswith("public/")

    public_artifacts = b"".join(path.read_bytes() for path in public_paths.values())
    record_artifacts = (
        public_artifacts
        + (destination / "records" / "key-policy.json").read_bytes()
        + record_path.read_bytes()
    )
    record_artifact_text = record_artifacts.decode("utf-8", errors="ignore")
    for private_path in (*ssh_private_paths.values(), *pem_private_paths.values()):
        private_bytes = private_path.read_bytes()
        assert private_bytes not in record_artifacts
        assert hashlib.sha256(private_bytes).hexdigest() not in record_artifact_text

    acl_check = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            """
$Root = $env:TIDETRACKER_TEST_ACL_ROOT
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($user, 'S-1-5-18')
$paths = @((Get-Item -LiteralPath $Root -Force)) + @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
foreach ($item in $paths) {
    $acl = Get-Acl -LiteralPath $item.FullName
    if (-not $acl.AreAccessRulesProtected) { throw "unprotected ACL: $($item.FullName)" }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -cne $user) { throw "wrong owner: $($item.FullName)" }
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    $rights = @{}
    foreach ($rule in $rules) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "non-Allow ACL: $($item.FullName)"
        }
        $sid = $rule.IdentityReference.Value
        if (-not $rights.ContainsKey($sid)) { $rights[$sid] = 0 }
        $rights[$sid] = [int]$rights[$sid] -bor [int]$rule.FileSystemRights
    }
    $sids = @($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
    if (@(Compare-Object $allowed $sids).Count -ne 0) { throw "wrong ACL: $($item.FullName)" }
    $full = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($sid in $allowed) {
        if (-not $rights.ContainsKey($sid) -or (($rights[$sid] -band $full) -ne $full)) {
            throw "incomplete ACL rights: $($item.FullName)"
        }
    }
}
"ACL-PASS"
""",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "TIDETRACKER_TEST_ACL_ROOT": str(destination)},
    )
    assert acl_check.returncode == 0, acl_check.stderr
    assert acl_check.stdout.strip() == "ACL-PASS"

    before = {
        path.relative_to(destination).as_posix(): _sha256(path)
        for path in destination.rglob("*")
        if path.is_file()
    }
    second = _run(local_app_data, *arguments)
    after = {
        path.relative_to(destination).as_posix(): _sha256(path)
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert second.returncode != 0
    assert "Refusing to overwrite existing keystore" in second.stderr
    assert before == after
    assert not list(destination.parent.glob("v1.partial.*"))
