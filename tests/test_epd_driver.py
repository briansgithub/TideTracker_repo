from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest


class Spi:
    def writebytes2(self, data: object) -> None:
        del data


class PackageModule(types.ModuleType):
    __path__: list[str]


class ConfigModule(types.ModuleType):
    RST_PIN: int
    DC_PIN: int
    BUSY_PIN: int
    CS_PIN: int
    SPI: Spi
    digital_write: Callable[..., None]
    digital_read: Callable[..., int]
    spi_writebyte: Callable[..., None]
    delay_ms: Callable[..., None]


def load_driver(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    package = PackageModule("test_waveshare")
    package.__path__ = []
    config = ConfigModule("test_waveshare.epdconfig")
    config.RST_PIN = 17
    config.DC_PIN = 25
    config.BUSY_PIN = 24
    config.CS_PIN = 8
    config.SPI = Spi()
    config.digital_write = lambda *_: None
    config.digital_read = lambda *_: 0
    config.spi_writebyte = lambda *_: None
    config.delay_ms = lambda *_: None
    monkeypatch.setitem(sys.modules, "test_waveshare", package)
    monkeypatch.setitem(sys.modules, "test_waveshare.epdconfig", config)
    path = (
        Path(__file__).parents[1]
        / "e-ink_lib"
        / "waveshare_epd"
        / "epd7in5_V2.py"
    )
    spec = importlib.util.spec_from_file_location("test_waveshare.epd7in5_V2", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_busy_wait_has_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_driver(monkeypatch)
    ticks = iter((0.0, 0.0, 0.06, 0.06))
    driver = module.EPD(busy_timeout_seconds=0.05, monotonic=lambda: next(ticks))

    with pytest.raises(module.DisplayTimeoutError, match="BUSY remained asserted"):
        driver.ReadBusy()
