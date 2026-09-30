"""FastMCP server construction from `McpSettings`, plus the injected auth and middleware."""

import time
from collections.abc import Sequence

import structlog
from fastmcp import FastMCP
from fastmcp.exceptions import FastMCPError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from mcp import MCPError

from app.core.logging import get_logger
from app.core.settings import Settings
from app.tools.base import Tool, Toolset, ToolsetSource

log = get_logger("app.mcp")


class McpLoggingMiddleware(Middleware):
	"""One structured log line per MCP request, with method, component, session and duration.

	Binds the same fields to the log context so logs emitted inside tools carry them too.
	"""

	async def on_request(self, context: MiddlewareContext, call_next: CallNext):
		fields = {"mcp_method": context.method}
		if target := getattr(context.message, "name", None) or getattr(context.message, "uri", None):
			fields["mcp_target"] = str(target)
		if context.fastmcp_context is not None:
			try:
				fields["mcp_session_id"] = context.fastmcp_context.session_id
			except RuntimeError:
				pass

		start = time.perf_counter()
		with structlog.contextvars.bound_contextvars(**fields):
			try:
				result = await call_next(context)
			except (FastMCPError, MCPError) as exc:
				# Expected, client-facing errors (ToolError, rate limits, scope errors, ...): no traceback.
				log.warning("mcp.request", status="error", error=str(exc), duration_ms=self._ms(start))
				raise
			except Exception:
				log.exception("mcp.request", status="error", duration_ms=self._ms(start))
				raise
			log.info("mcp.request", status="ok", duration_ms=self._ms(start))
			return result

	@staticmethod
	def _ms(start: float) -> float:
		return round((time.perf_counter() - start) * 1000, 2)


class McpServerFactory:
	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.config = settings.mcp

	def create(
		self,
		toolsets: Sequence[ToolsetSource],
		auth: AuthProvider | None = None,
		middleware: Sequence[Middleware] = (),
	) -> FastMCP:
		"""
		Builds the FastMCP server.

		Middleware runs outermost first: request logging, rate limiting (when enabled in
		settings), then ``middleware`` in the order given. Auth runs before all of it, at
		the HTTP layer.

		:param toolsets: Toolset classes or instances, and function ``Tool`` s, to register.
		:type toolsets: Sequence[ToolsetSource]
		:param auth: Auth for the MCP endpoint; ``None`` leaves it open.
		:type auth: AuthProvider | None
		:param middleware: Additional FastMCP middleware.
		:type middleware: Sequence[Middleware]
		:rtype: FastMCP
		"""
		server = FastMCP(
			name=self.config.name,
			instructions=self.config.instructions,
			version=self.settings.version,
			auth=auth,
			middleware=[*self._builtin_middleware(), *middleware],
			mask_error_details=self.config.mask_error_details,
			strict_input_validation=self.config.strict_input_validation,
			on_duplicate="error",
		)
		for source in toolsets:
			self._component(source).register(server)
		return server

	def _component(self, source: ToolsetSource) -> Toolset | Tool:
		if isinstance(source, Toolset | Tool):
			return source
		if isinstance(source, type) and issubclass(source, Toolset):
			return source(self.settings)
		raise TypeError(f"expected a Toolset class or instance, or a Tool; got {source!r}")

	def http_app(self, server: FastMCP) -> StarletteWithLifespan:
		stateless = self.config.stateless_http
		return server.http_app(
			path=self.config.path,
			transport="http",
			stateless_http=stateless,
			json_response=self.config.json_response,
			session_idle_timeout=None if stateless else self.config.session_idle_timeout,
			host_origin_protection=self.config.host_origin_protection,
			allowed_hosts=self.config.allowed_hosts or None,
			allowed_origins=self.config.allowed_origins or None,
		)

	def _builtin_middleware(self) -> list[Middleware]:
		middleware: list[Middleware] = [McpLoggingMiddleware()]
		if self.config.rate_limit.enabled:
			middleware.append(
				RateLimitingMiddleware(
					max_requests_per_second=self.config.rate_limit.requests_per_second,
					burst_capacity=self.config.rate_limit.burst_capacity,
					get_client_id=_rate_limit_key,
				)
			)
		return middleware


def _rate_limit_key(_context: MiddlewareContext) -> str:
	"""One bucket per authenticated client, else per client IP (the real IP when proxy headers are trusted)."""
	if token := get_access_token():
		return f"client:{token.client_id}"
	try:
		client = get_http_request().client
	except RuntimeError:
		return "local"
	return f"ip:{client.host}" if client else "unknown"
