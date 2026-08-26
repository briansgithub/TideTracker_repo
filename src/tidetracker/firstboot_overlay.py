"""Power-cut-safe activation of the field image's tmpfs root overlay."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

OVERLAY_ARGUMENT = "overlayroot=tmpfs:recurse=0"
DISABLED_OVERLAY_CONFIG = 'overlayroot=""\n'
_MAX_CONFIGURATION_BYTES = 1024 * 1024


class OverlayConfigurationError(RuntimeError):
    pass


def _checked_read(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            payload = os.read(descriptor, _MAX_CONFIGURATION_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OverlayConfigurationError(f"cannot safely read required configuration: {path}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_size != len(payload)
        or len(payload) > _MAX_CONFIGURATION_BYTES
    ):
        raise OverlayConfigurationError(f"required configuration is not stable: {path}")
    try:
        return payload.decode("utf-8"), stat.S_IMODE(before.st_mode)
    except UnicodeError as exc:
        raise OverlayConfigurationError(f"required configuration is not UTF-8: {path}") from exc


def _atomic_write(path: Path, content: str, mode: int) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OverlayConfigurationError(f"cannot inspect configuration target: {path}") from exc
    else:
        if not stat.S_ISREG(current.st_mode) or path.is_symlink():
            raise OverlayConfigurationError(f"refusing to replace non-regular configuration: {path}")
    temporary = path.with_name(f".{path.name}.tidetracker.tmp")
    _remove_owned_temporary(temporary)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError as exc:
        raise OverlayConfigurationError(f"cannot create private configuration temp: {path}") from exc
    try:
        os.fchmod(descriptor, mode)
        if hasattr(os, "fchown"):
            os.fchown(descriptor, 0, 0)
        payload = content.encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short configuration write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise OverlayConfigurationError(f"configuration update was not durable: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _remove_owned_temporary(temporary)


def _remove_owned_temporary(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OverlayConfigurationError("cannot inspect an overlay configuration temp") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        raise OverlayConfigurationError("overlay configuration temp is malformed or unsafe")
    try:
        path.unlink()
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise OverlayConfigurationError("overlay configuration temp cleanup was not durable") from exc


def configure_overlay_files(
    fstab_path: Path,
    overlay_config_path: Path,
    cmdline_path: Path,
    *,
    after_write: Callable[[str], None] | None = None,
) -> None:
    """Make the kernel command line the sole overlay activation commit point."""

    fstab, fstab_mode = _checked_read(fstab_path)
    _overlay_config, overlay_mode = _checked_read(overlay_config_path)
    cmdline, cmdline_mode = _checked_read(cmdline_path)

    rewritten_fstab: list[str] = []
    boot_rows = 0
    for line in fstab.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            rewritten_fstab.append(line)
            continue
        fields = line.split()
        if len(fields) < 4 or fields[1] not in {"/boot", "/boot/firmware"}:
            rewritten_fstab.append(line)
            continue
        boot_rows += 1
        options = [item for item in fields[3].split(",") if item not in {"ro", "rw"}]
        options.append("ro")
        fields[3] = ",".join(options)
        rewritten_fstab.append("\t".join(fields))
    if boot_rows != 1:
        raise OverlayConfigurationError(
            "exactly one boot filesystem entry is required in /etc/fstab"
        )

    if len(cmdline.splitlines()) != 1:
        raise OverlayConfigurationError("boot command line must contain exactly one line")
    rewritten_tokens: list[str] = []
    overlay_tokens = 0
    for token in cmdline.split():
        if token == "overlayroot=tmpfs" or token.startswith("overlayroot=tmpfs:"):
            overlay_tokens += 1
            rewritten_tokens.append(OVERLAY_ARGUMENT)
        elif token.startswith("overlayroot="):
            raise OverlayConfigurationError(f"unexpected overlayroot boot option: {token}")
        else:
            rewritten_tokens.append(token)
    if overlay_tokens > 1:
        raise OverlayConfigurationError("at most one overlayroot boot option is allowed")
    if overlay_tokens == 0:
        rewritten_tokens.insert(0, OVERLAY_ARGUMENT)

    # The config file remains explicitly non-activating. Therefore a power cut
    # before the final cmdline rename always returns to the writable lower root.
    updates = (
        ("fstab", fstab_path, "\n".join(rewritten_fstab) + "\n", fstab_mode),
        ("overlay_config", overlay_config_path, DISABLED_OVERLAY_CONFIG, overlay_mode),
        ("cmdline", cmdline_path, " ".join(rewritten_tokens) + "\n", cmdline_mode),
    )
    for label, path, content, mode in updates:
        _atomic_write(path, content, mode)
        if after_write is not None:
            after_write(label)


def main(arguments: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fstab", type=Path)
    parser.add_argument("overlay_config", type=Path)
    parser.add_argument("cmdline", type=Path)
    args = parser.parse_args(arguments)
    configure_overlay_files(args.fstab, args.overlay_config, args.cmdline)
    return 0


if __name__ == "__main__":  # pragma: no cover - field entry point
    raise SystemExit(main())
