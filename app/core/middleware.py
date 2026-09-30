import re
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"
# Accept caller-supplied ids only if they look like ids, so they can't inject into logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

log = get_logger("app.access")

_request_base_url: ContextVar[str] = ContextVar("request_base_url", default="")


def request_base_url() -> str:
	"""
	Base URL of the HTTP request being handled (scheme, host and root path), for building
	absolute links in API and MCP responses. Empty outside a request.

	Honors X-Forwarded-* headers from trusted proxies (``server.forwarded_allow_ips``).
	"""
	return _request_base_url.get()


class RequestContextMiddleware:
	"""Assigns a request id, binds it to the log context, and writes one access log line per request.

	Pure ASGI (not BaseHTTPMiddleware) so MCP's streaming responses pass through untouched.
	Unhandled exceptions are logged once, with traceback and request id, and turned into a
	JSON 500 that carries the request id back to the caller.
	"""

	def __init__(self, app: ASGIApp, *, access_log: bool = True, exclude_paths: list[str] | None = None) -> None:
		self.app = app
		self.access_log = access_log
		self.exclude_paths = frozenset(exclude_paths or ())

	async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
		if scope["type"] != "http":
			await self.app(scope, receive, send)
			return

		request_id = self._request_id(scope)
		scope.setdefault("state", {})["request_id"] = request_id
		_request_base_url.set(str(Request(scope).base_url).rstrip("/"))
		structlog.contextvars.clear_contextvars()
		structlog.contextvars.bind_contextvars(request_id=request_id)

		status_code = 500
		response_started = False
		start = time.perf_counter()

		async def send_wrapper(message: Message) -> None:
			nonlocal status_code, response_started
			if message["type"] == "http.response.start":
				response_started = True
				status_code = int(message["status"])  # the MCP SDK sends HTTPStatus enums
				MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
			await send(message)

		try:
			await self.app(scope, receive, send_wrapper)
		except Exception:
			log.exception("http.unhandled_exception", method=scope["method"], path=scope["path"])
			if response_started:
				raise
			response = JSONResponse(
				{"detail": "Internal Server Error", "request_id": request_id},
				status_code=500,
				headers={REQUEST_ID_HEADER: request_id},
			)
			await response(scope, receive, send)
		finally:
			if self.access_log and scope["path"] not in self.exclude_paths:
				self._log_access(scope, status_code, time.perf_counter() - start)

	@staticmethod
	def _request_id(scope: Scope) -> str:
		for name, value in scope["headers"]:
			if name == b"x-request-id":
				candidate = value.decode("latin-1")
				if _VALID_REQUEST_ID.match(candidate):
					return candidate
				break
		return uuid.uuid4().hex

	@staticmethod
	def _log_access(scope: Scope, status_code: int, elapsed: float) -> None:
		client = scope.get("client")
		fields = {
			"method": scope["method"],
			"path": scope["path"],
			"status": status_code,
			"duration_ms": round(elapsed * 1000, 2),
			"client_ip": client[0] if client else None,
		}
		if status_code >= 500:
			log.error("http.request", **fields)
		elif status_code >= 400:
			log.warning("http.request", **fields)
		else:
			log.info("http.request", **fields)
