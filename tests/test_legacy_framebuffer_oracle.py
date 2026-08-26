from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from tidetracker.display import FRAME_BYTES, FRAME_HEIGHT, FRAME_WIDTH

REPOSITORY = Path(__file__).parents[1]
ORACLE = Path(__file__).parent / "fixtures" / "legacy_framebuffer.json"


def _legacy_controller_frame(bitmap: Path) -> bytes:
    with Image.open(bitmap) as source:
        rotated = source.convert("1").transpose(Image.Transpose.ROTATE_180)
        assert rotated.size == (FRAME_WIDTH, FRAME_HEIGHT)
        packed = bytearray(rotated.tobytes("raw"))
    for index in range(len(packed)):
        packed[index] ^= 0xFF
    return bytes(packed)


def test_checked_in_legacy_framebuffer_oracle_is_exact_and_controller_sized() -> None:
    metadata = json.loads(ORACLE.read_text(encoding="utf-8"))
    bitmap = REPOSITORY / str(metadata["source_bitmap"])

    assert hashlib.sha256(bitmap.read_bytes()).hexdigest() == metadata["source_bitmap_sha256"]
    framebuffer = _legacy_controller_frame(bitmap)

    assert len(framebuffer) == FRAME_BYTES == metadata["controller_frame_bytes"]
    assert hashlib.sha256(framebuffer).hexdigest() == metadata["controller_frame_sha256"]
