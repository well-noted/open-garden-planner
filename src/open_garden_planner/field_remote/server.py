"""Field Remote HTTPS server: lifecycle, request guards and the stub route table. No Qt.

Runs uvicorn on its own daemon thread and its own port, never mounted on the Agent API.
Every request passes ``FieldRemoteGuard`` before any route runs; later packages add routes to
``DEFAULT_ROUTES`` (through ``extra_routes``) and inherit the guards without touching them.

The phone never edits the plan: routes added later write only field records, and only while
``ServerState.records_enabled`` is on (``require_records_enabled``).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from open_garden_planner.field_remote import auth, limits

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from open_garden_planner.field_remote.tls import TlsMaterial

logger = logging.getLogger(__name__)

SERVER_THREAD_NAME = "field-remote"
API_VERSION = 1
_READY_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 3.0
#: Slows a guessing client without blocking the loop (design 1.5).
FAILURE_DELAY_S = 0.25
#: Paths served without a token. Only the page that has to load BEFORE it can read the token
#: from the URL fragment belongs here; every /api/ route needs the header.
PUBLIC_PATHS = frozenset({"/"})
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

#: Sent on every response, including rejections. No CORS headers are ever sent (a cross-origin
#: page must not read a response), and nothing is cached (responses carry plan data).
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'",
    ),
)


class PortInUseError(RuntimeError):
    """The configured Field Remote port is already bound."""

    def __init__(self, port: int) -> None:
        super().__init__(f"Port {port} is already in use")
        self.port = port


def check_bind_address(bind: str) -> str:
    """Return the normalised bind address, or raise ``ValueError``.

    A specific private, link-local, Tailscale or loopback address only. ``0.0.0.0``/``::`` would
    also answer on a public Wi-Fi, and a public address is never a phone-on-my-network address,
    so neither is accepted (design 1.2). Wildcard binding is deliberately not offered here.
    """
    try:
        address = ipaddress.ip_address(bind.strip())
    except ValueError:
        raise ValueError(f"{bind!r} is not an IP address") from None
    if address.is_unspecified:
        raise ValueError("A specific address is required; 0.0.0.0 and :: are not accepted")
    local = address.is_loopback or address.is_private or address.is_link_local
    if not (local or (address.version == 4 and address in _TAILSCALE_NET)):
        raise ValueError(f"{address} is not a loopback, private-network or Tailscale address")
    return str(address)


@dataclass
class ServerState:
    """Live values the server thread reads. Written only through the host on the Qt thread;
    a plain attribute swap is atomic in CPython, so rotation takes effect on the next request."""

    token: str
    records_enabled: bool = False


def require_records_enabled(request: Request) -> Response | None:
    """For record-writing routes: None when allowed, else the 403 to return (design 1.5)."""
    from starlette.responses import JSONResponse

    state: ServerState = request.app.state.field_remote
    if state.records_enabled:
        return None
    return JSONResponse(
        {"error": "records_disabled", "detail": "Saving records from the phone is turned off on the computer."},
        status_code=403,
    )


async def _status(request: Request) -> Response:
    from starlette.responses import JSONResponse

    state: ServerState = request.app.state.field_remote
    # Nothing here that names the plan, a path or the token.
    return JSONResponse(
        {
            "app": "open-garden-planner-field-remote",
            "api": API_VERSION,
            "records_enabled": state.records_enabled,
            "server_time": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        }
    )


async def _index(_request: Request) -> Response:
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("Open Garden Planner Field Remote. The phone page arrives in a later version.")


def default_routes() -> list[BaseRoute]:
    """The stub route table later packages extend (``extra_routes``)."""
    from starlette.routing import Route

    return [
        Route("/", _index, methods=["GET"]),
        Route("/api/status", _status, methods=["GET"]),
    ]


class FieldRemoteGuard:
    """Pure-ASGI guard in front of the router. Each check names the threat it answers.

    Order matters: cheap, credential-free rejections first, then lockout, then the token.
    """

    def __init__(
        self,
        app: Any,
        state: ServerState,
        hosts: frozenset[str],
        *,
        clock: Callable[[], float] = time.monotonic,
        failure_delay_s: float = FAILURE_DELAY_S,
        max_body: int = limits.MAX_BODY_BYTES,
    ) -> None:
        self._app = app
        self._state = state
        self._hosts = hosts
        self._max_body = max_body
        self._failure_delay = failure_delay_s
        self.lockout = limits.FailureLockout(clock=clock)
        self.rate = limits.RateLimiter(clock=clock)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket in this server
            return

        started = False

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": [*message.get("headers", []), *SECURITY_HEADERS]}
            await send(message)

        rejection = await self._check(scope)
        if rejection is not None:
            await rejection(scope, receive, guarded_send)
            return
        try:
            await self._app(scope, limits.limit_receive(receive, self._max_body), guarded_send)
        except limits.BodyTooLarge:
            if not started:  # a streamed body that outgrew the cap; nothing was answered yet
                await _reject(413, "body_too_large")(scope, receive, guarded_send)

    async def _check(self, scope: dict[str, Any]) -> Any:
        headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
        # DNS rebinding: a page on attacker.example that resolves to this IP sends
        # "Host: attacker.example", which is not in the allowlist.
        if auth.host_header(headers) not in self._hosts:
            return _reject(403, "bad_host")
        # CSRF / cross-site reads: a foreign page's request carries its own Origin.
        if not auth.origin_allowed(auth.origin_header(headers), self._hosts):
            return _reject(403, "bad_origin")
        # A token in the URL would be logged and kept in history: refuse, even a correct one.
        if auth.query_carries_token(scope.get("query_string") or b""):
            return _reject(400, "token_in_url", "Send the token in the Authorization header.")
        length = limits.declared_length(headers)
        if length is not None and (length < 0 or length > self._max_body):
            return _reject(413, "body_too_large")
        if scope["path"] in PUBLIC_PATHS:
            return None
        client = (scope.get("client") or ("unknown", 0))[0]
        # Token guessing from the LAN: lock the address out after repeated failures.
        if self.lockout.is_locked(client):
            return _reject(429, "locked_out", extra=[(b"retry-after", str(int(limits.LOCKOUT_SECONDS)).encode())])
        if not auth.token_matches(auth.bearer_from_headers(headers), self._state.token):
            self.lockout.record_failure(client)
            await asyncio.sleep(self._failure_delay)
            return _reject(401, "unauthorized", extra=[(b"www-authenticate", b"Bearer")])
        self.lockout.reset(client)
        # A paired phone (or a leaked token) flooding the desktop.
        if not self.rate.allow(scope["method"]):
            return _reject(429, "rate_limited", extra=[(b"retry-after", b"1")])
        return None


def _reject(
    status: int, code: str, detail: str = "", extra: list[tuple[bytes, bytes]] | None = None
) -> Callable[..., Awaitable[None]]:
    from starlette.responses import JSONResponse

    response = JSONResponse({"error": code, "detail": detail} if detail else {"error": code}, status_code=status)
    for key, value in extra or []:
        response.raw_headers.append((key, value))
    return response


def build_app(
    state: ServerState,
    bind: str,
    port: int,
    *,
    extra_routes: Sequence[BaseRoute] = (),
    extra_names: tuple[str, ...] = (),
    guard_kwargs: dict[str, Any] | None = None,
) -> Any:
    """The ASGI app: guard in front of a Starlette router. Also used by the tests directly."""
    from starlette.applications import Starlette

    router = Starlette(routes=[*default_routes(), *extra_routes])
    router.state.field_remote = state
    hosts = auth.allowed_hosts(bind, port, extra_names)
    return FieldRemoteGuard(router, state, hosts, **(guard_kwargs or {}))


class FieldRemoteServer:
    """Start/stop wrapper around uvicorn with TLS on a daemon thread (idempotent)."""

    def __init__(
        self,
        state: ServerState,
        tls: TlsMaterial,
        *,
        bind: str = "127.0.0.1",
        port: int = 8766,
        extra_routes: Sequence[BaseRoute] = (),
        extra_names: tuple[str, ...] = (),
    ) -> None:
        self._state = state
        self._tls = tls
        self._bind = check_bind_address(bind)
        self._port = port
        self._extra_routes = extra_routes
        self._extra_names = extra_names
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._lock = threading.Lock()

    @property
    def bind(self) -> str:
        return self._bind

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._check_port_free()
            self._start_locked()

    def _check_port_free(self) -> None:
        family = socket.AF_INET6 if ":" in self._bind else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.bind((self._bind, self._port))
        except OSError as exc:
            raise PortInUseError(self._port) from exc
        finally:
            probe.close()

    def _start_locked(self) -> None:
        import uvicorn

        app = build_app(
            self._state, self._bind, self._port, extra_routes=self._extra_routes, extra_names=self._extra_names
        )
        config = uvicorn.Config(
            app,
            host=self._bind,
            port=self._port,
            ssl_certfile=str(self._tls.cert_path),
            ssl_keyfile=str(self._tls.key_path),
            log_level="warning",
            access_log=False,  # request lines are not logged
            log_config=None,  # as the Agent API: no stdout-dependent logging in a windowed exe (#291)
            lifespan="on",
            server_header=False,
            ws="none",
            limit_concurrency=32,  # flooding: excess connections get 503
            timeout_keep_alive=5,
        )
        server = uvicorn.Server(config)
        self._server = server
        thread = threading.Thread(target=self._run, args=(server,), name=SERVER_THREAD_NAME, daemon=True)
        self._thread = thread
        thread.start()
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if not thread.is_alive():
                self._thread = None
                self._server = None
                raise RuntimeError("Field Remote server failed to start")
            if getattr(server, "started", False):
                return
            time.sleep(0.02)
        logger.warning("Field Remote server did not report ready within %.0fs", _READY_TIMEOUT_S)

    @staticmethod
    def _run(server: Any) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except Exception:
            logger.exception("Field Remote server crashed")
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=_STOP_TIMEOUT_S)
                if thread.is_alive():
                    server.force_exit = True  # drop open connections
                    thread.join(timeout=_STOP_TIMEOUT_S)
                if thread.is_alive():
                    logger.error("Field Remote server thread did not stop")
            self._thread = None
            self._server = None
