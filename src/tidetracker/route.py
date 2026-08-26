"""Fail-closed production route inspection for NOAA traffic."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlsplit

from .noaa import NOAA_ENDPOINT

_ADDRESS_TOKEN = re.compile(r"(?<![0-9A-Fa-f:.])[0-9A-Fa-f:.]+(?![0-9A-Fa-f:.])")


class ProductionRouteError(RuntimeError):
    """The actual route to the production endpoint cannot be proven safe."""


class CommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _command_runner(
    args: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=timeout,
    )


class ProductionRoutePort(Protocol):
    def interface_for_endpoint(self, *, timeout_seconds: float) -> str: ...


class SystemProductionRoute:
    """Resolve NOAA on wlan0, then inspect every resulting kernel route.

    ``resolvectl --interface=wlan0`` prevents the route probe itself from
    leaking DNS onto the USB management link. Both IPv4 and IPv6 answers are
    checked because the HTTPS client may select either family.
    """

    def __init__(
        self,
        endpoint: str = NOAA_ENDPOINT,
        *,
        expected_interface: str = "wlan0",
        runner: CommandRunner = _command_runner,
        resolvectl_binary: str = "/usr/bin/resolvectl",
        ip_binary: str = "/usr/sbin/ip",
    ) -> None:
        hostname = urlsplit(endpoint).hostname
        if not hostname:
            raise ValueError("production endpoint must contain a hostname")
        if not expected_interface or any(char.isspace() for char in expected_interface):
            raise ValueError("expected route interface is invalid")
        self.hostname = hostname
        self.expected_interface = expected_interface
        self.runner = runner
        self.resolvectl_binary = resolvectl_binary
        self.ip_binary = ip_binary

    @staticmethod
    def _addresses(output: str) -> tuple[str, ...]:
        addresses: list[str] = []
        for match in _ADDRESS_TOKEN.finditer(output):
            token = match.group(0).strip("[](),")
            if "." not in token and ":" not in token:
                continue
            try:
                address = str(ipaddress.ip_address(token))
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ProductionRouteError("wlan0 DNS returned no production endpoint address")
        return tuple(addresses)

    def _run(self, args: list[str], *, timeout: float, purpose: str) -> str:
        try:
            completed = self.runner(args, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProductionRouteError(f"{purpose} failed: {exc}") from exc
        if completed.returncode != 0:
            detail = " ".join(completed.stderr.split())[:200]
            raise ProductionRouteError(
                f"{purpose} exited {completed.returncode}: {detail}"
            )
        return completed.stdout

    def interface_for_endpoint(self, *, timeout_seconds: float) -> str:
        if timeout_seconds <= 0:
            raise ProductionRouteError("production-route timeout budget is exhausted")
        dns_budget = min(5.0, timeout_seconds / 2.0)
        raw_addresses = self._run(
            [
                self.resolvectl_binary,
                "query",
                f"--interface={self.expected_interface}",
                "--protocol=dns",
                "--type=A",
                "--type=AAAA",
                "--legend=no",
                self.hostname,
            ],
            timeout=dns_budget,
            purpose="interface-bound production DNS query",
        )
        addresses = self._addresses(raw_addresses)
        remaining = timeout_seconds - dns_budget
        if remaining <= 0:
            raise ProductionRouteError("production-route timeout budget is exhausted")
        interfaces: set[str] = set()
        per_route = remaining / len(addresses)
        for address in addresses:
            raw_route = self._run(
                [self.ip_binary, "-json", "route", "get", address],
                timeout=per_route,
                purpose=f"production route lookup for {address}",
            )
            try:
                routes = json.loads(raw_route)
                interface = routes[0]["dev"]
            except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ProductionRouteError("ip route get returned an invalid result") from exc
            if not isinstance(interface, str) or not interface:
                raise ProductionRouteError("ip route get did not identify an interface")
            interfaces.add(interface)
        if interfaces != {self.expected_interface}:
            rendered = ",".join(sorted(interfaces)) or "unknown"
            raise ProductionRouteError(
                f"production route must use {self.expected_interface}; got {rendered}"
            )
        return self.expected_interface


class StaticProductionRoute:
    """No-command route adapter for deterministic tests and simulation."""

    def __init__(self, interface: str = "wlan0") -> None:
        self.interface = interface

    def interface_for_endpoint(self, *, timeout_seconds: float) -> str:
        if timeout_seconds <= 0:
            raise ProductionRouteError("production-route timeout budget is exhausted")
        return self.interface


__all__ = [
    "ProductionRouteError",
    "ProductionRoutePort",
    "StaticProductionRoute",
    "SystemProductionRoute",
]
