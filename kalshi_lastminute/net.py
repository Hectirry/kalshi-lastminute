"""Sesión HTTP con resolución DNS propia para los dominios configurados.

El DNS del proveedor de internet devuelve un sumidero para kalshi.com
(docs/RESEARCH.md §0). Por decisión explícita del dueño del proyecto, el bot
resuelve SOLO los dominios de `network.dns_override.hosts` mediante
DNS-over-HTTPS; el resto usa el DNS del sistema, y Windows no se modifica.
"""
from __future__ import annotations

import socket
import time

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver


def matches(host: str, suffixes: list[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


class OverrideResolver(AbstractResolver):
    def __init__(self, suffixes: list[str], doh_url: str, ttl_s: float = 300.0) -> None:
        self.suffixes = suffixes
        self.doh_url = doh_url
        self.ttl_s = ttl_s
        self._default = ThreadedResolver()
        self._cache: dict[str, tuple[float, list[str]]] = {}

    async def _lookup(self, host: str) -> list[str]:
        hit = self._cache.get(host)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        # doh_url apunta a una IP, así que esta consulta no necesita DNS.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(self.doh_url, params={"name": host, "type": "A"},
                             headers={"accept": "application/dns-json"}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
        if not ips:
            if hit:  # mejor una respuesta vieja que ninguna
                return hit[1]
            raise OSError(f"DoH sin registros A para {host}")
        self._cache[host] = (time.monotonic(), ips)
        return ips

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict]:
        if not matches(host, self.suffixes):
            return await self._default.resolve(host, port, family)
        return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST} for ip in await self._lookup(host)]

    async def close(self) -> None:
        await self._default.close()


def make_session(cfg: dict) -> aiohttp.ClientSession:
    dns = (cfg.get("network") or {}).get("dns_override") or {}
    if not dns.get("enabled"):
        return aiohttp.ClientSession()
    resolver = OverrideResolver(dns["hosts"], dns["doh_url"])
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=resolver))
