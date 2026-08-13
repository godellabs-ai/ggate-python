"""mTLS IPC transport to the local ggate-agent.

Mirrors the Rust collector client (``shared/ggate-ipc``): resolve the endpoint from
``~/.ggate/agent.json`` when present, otherwise probe the 47640-47644 rendezvous
window with the standard on-disk material. Every connection pins the agent's CA,
presents the collector client certificate (the agent requires it), then carries
exactly one exchange: an auth-key line, one JSON request line, one JSON response
line. A wrong or non-ggate listener fails the pinned-CA handshake, so probing is
safe.

Endpoint material (discovery file, CA, client cert, SSL context) is cached and
re-validated against the discovery file's mtime, so steady-state requests skip all
filesystem work except the socket itself.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..exceptions import GgateTransportError
from .config import Config

# Collector<->agent rendezvous window. Keep in lockstep with shared/ggate-ipc/src/endpoint.rs.
AGENT_IPC_PORT_BASE = 47640
AGENT_IPC_PORT_WINDOW = 5

# The agent answers with a single JSON line; cap reads so a misbehaving peer cannot balloon memory.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ResolvedEndpoint:
    """Ready-to-dial material, mirroring the Rust ``Resolved`` struct.

    ``addrs`` holds candidates in dial order: the discovery-file address when the
    file exists, else every port in the rendezvous window.
    """

    addrs: Tuple[Tuple[str, int], ...]
    ca_path: Path
    client_cert_path: Path
    client_key_path: Path
    auth_key: str


class Transport:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[Optional[Tuple[int, int]], ResolvedEndpoint, ssl.SSLContext]] = None

    def request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        endpoint, context = self._resolve()
        wire = _encode(endpoint.auth_key, request)
        errors = []
        for host, port in endpoint.addrs:
            try:
                return self._request_at(host, port, context, wire)
            except GgateTransportError as exc:
                errors.append(f"{host}:{port}: {exc}")
        self._invalidate()
        raise GgateTransportError("; ".join(errors))

    async def request_async(self, request: Dict[str, Any]) -> Dict[str, Any]:
        endpoint, context = self._resolve()
        wire = _encode(endpoint.auth_key, request)
        errors = []
        for host, port in endpoint.addrs:
            try:
                return await self._request_at_async(host, port, context, wire)
            except GgateTransportError as exc:
                errors.append(f"{host}:{port}: {exc}")
        self._invalidate()
        raise GgateTransportError("; ".join(errors))

    def _request_at(self, host: str, port: int, context: ssl.SSLContext, wire: bytes) -> Dict[str, Any]:
        try:
            with socket.create_connection((host, port), timeout=self.config.timeout) as sock:
                sock.settimeout(self.config.timeout)
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    tls.sendall(wire)
                    line = _read_line(tls)
            return json.loads(line)
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise GgateTransportError(str(exc) or type(exc).__name__) from exc

    async def _request_at_async(self, host: str, port: int, context: ssl.SSLContext, wire: bytes) -> Dict[str, Any]:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host=host, port=port, ssl=context, server_hostname=host),
                timeout=self.config.timeout,
            )
            writer.write(wire)
            await asyncio.wait_for(writer.drain(), timeout=self.config.timeout)
            line = await asyncio.wait_for(reader.readline(), timeout=self.config.timeout)
            if not line:
                raise GgateTransportError("agent closed connection without response")
            return json.loads(line)
        except (OSError, ssl.SSLError, asyncio.TimeoutError, ValueError) as exc:
            raise GgateTransportError(str(exc) or type(exc).__name__) from exc
        finally:
            if writer is not None:
                writer.close()

    # ---- endpoint resolution ----

    def _resolve(self) -> Tuple[ResolvedEndpoint, ssl.SSLContext]:
        sig = _file_signature(self.config.agent_json)
        with self._lock:
            if self._cached is not None and self._cached[0] == sig:
                return self._cached[1], self._cached[2]
        endpoint = self._resolve_uncached()
        try:
            context = ssl.create_default_context(cafile=str(endpoint.ca_path))
            context.load_cert_chain(str(endpoint.client_cert_path), str(endpoint.client_key_path))
        except (OSError, ssl.SSLError) as exc:
            raise GgateTransportError(f"loading agent TLS material: {exc}") from exc
        with self._lock:
            self._cached = (sig, endpoint, context)
        return endpoint, context

    def _invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def _resolve_uncached(self) -> ResolvedEndpoint:
        path = self.config.agent_json
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            host, port = _split_addr(raw["addr"])
            ca_path = Path(raw["ca_path"]).expanduser()
            auth_key = Path(raw["key_path"]).expanduser().read_text(encoding="utf-8").strip()
            cert, key = raw.get("client_cert_path"), raw.get("client_key_path")
            if cert and key:
                cert_path, key_path = Path(cert).expanduser(), Path(key).expanduser()
            else:
                # Pre-mTLS discovery file: client material sits beside the CA.
                cert_path, key_path = ca_path.parent / "client.pem", ca_path.parent / "client.key"
            return ResolvedEndpoint(((host, port),), ca_path, cert_path, key_path, auth_key)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self._resolve_from_window(exc)

    def _resolve_from_window(self, cause: Exception) -> ResolvedEndpoint:
        """No usable discovery file: dial the whole rendezvous window with on-disk material."""
        directory = self._material_dir()
        if directory is None:
            raise GgateTransportError(
                f"agent not running (no {self.config.agent_json} and no TLS material on disk): {cause}"
            ) from cause
        try:
            auth_key = (directory / "collector.key").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GgateTransportError(f"reading auth key {directory / 'collector.key'}: {exc}") from exc
        addrs = tuple(("127.0.0.1", AGENT_IPC_PORT_BASE + i) for i in range(AGENT_IPC_PORT_WINDOW))
        return ResolvedEndpoint(
            addrs,
            directory / "ca.pem",
            directory / "client.pem",
            directory / "client.key",
            auth_key,
        )

    def _material_dir(self) -> Optional[Path]:
        """Directory holding fallback TLS material: beside the discovery file (a shared agent
        publishes `ca.pem`/`client.pem`/`client.key`/`collector.key` next to `agent.json`), else the
        per-user default TLS dir."""
        for candidate in (self.config.agent_json.parent, self.config.tls_dir):
            if (candidate / "ca.pem").exists():
                return candidate
        return None


def _encode(auth_key: str, request: Dict[str, Any]) -> bytes:
    body = json.dumps(request, separators=(",", ":"), default=str)
    return f"{auth_key}\n{body}\n".encode("utf-8")


def _read_line(tls: ssl.SSLSocket) -> bytes:
    buffer = bytearray()
    while b"\n" not in buffer:
        chunk = tls.recv(65536)
        if not chunk:
            if not buffer:
                raise GgateTransportError("agent closed connection without response")
            break
        buffer.extend(chunk)
        if len(buffer) > _MAX_RESPONSE_BYTES:
            raise GgateTransportError("agent response exceeded size limit")
    return bytes(buffer.split(b"\n", 1)[0])


def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _split_addr(addr: str) -> Tuple[str, int]:
    if addr.startswith("["):
        host, _, tail = addr[1:].partition("]")
        return host, int(tail.lstrip(":"))
    host, _, port = addr.rpartition(":")
    if not host or not port:
        raise ValueError(f"invalid addr {addr!r}")
    return host, int(port)
