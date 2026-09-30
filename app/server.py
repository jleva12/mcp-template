from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Self

import uvicorn
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import Middleware as McpMiddleware
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.routing import Match, Mount
from starlette.types import ASGIApp, Scope

from app.core.auth import combine_auth, http_bearer_auth
from app.core.logging import configure_logging, get_logger
from app.core.mcp import McpServerFactory
from app.core.middleware import RequestContextMiddleware
from app.core.settings import Settings, get_settings
from app.routes.base import Route, Routes, RouteSource
from app.tools.base import Tool, ToolsetSource

log = get_logger("app.server")

type Lifespan = Callable[[Any], AbstractAsyncContextManager[Any]]


class _RootMount(Mount):
	"""Root mount that only claims paths the sub-app routes.

	A plain `Mount("/")` fully matches every path, so it would swallow FastAPI's JSON 404s
	and turn 405s on API routes (e.g. POST /health/live) into the sub-app's 404.
	"""

	def __init__(self, app: ASGIApp) -> None:
		super().__init__("/", app=app)

	def matches(self, scope: Scope) -> tuple[Match, Scope]:
		match, child_scope = super().matches(scope)
		if match is Match.NONE:
			return match, child_scope
		sub_scope = {**scope, **child_scope}
		if any(route.matches(sub_scope)[0] is not Match.NONE for route in self.routes):
			return match, child_scope
		return Match.NONE, {}


class ServerBuilder:
	"""Assembles the FastAPI app, HTTP routes, and the FastMCP server from settings.

	app = ServerBuilder().with_routes(HealthRoutes).with_toolsets(GreetingTools).build()

	Classes are constructed with settings. Pass instances instead when routes and
	toolsets need to share a dependency, e.g. a file store that a tool writes to and
	a download route reads from:

		files = FileStore(settings)
		ServerBuilder(settings)
			.with_routes(FileRoutes(settings, files))
			.with_toolsets(ReportTools(settings, files))

	Single endpoints can be plain functions, without a ``Routes`` class:

		ServerBuilder(settings).with_routes(Route("/status", get_status, "GET"))

	and single MCP tools plain functions, without a ``Toolset`` class:

		ServerBuilder(settings).with_toolsets(Tool(lookup_order))

	Auth, startup/shutdown hooks and custom middleware are injected too (``with_auth``,
	``with_lifespan``, ``with_mcp_middleware``, ``with_http_middleware``). Route classes with
	``requires_auth = True`` use the same auth as MCP. Request IDs, access logs, CORS, MCP request logs and rate
	limiting are always installed and configured by settings.
	"""

	def __init__(self, settings: Settings | None = None) -> None:
		self.settings = settings or get_settings()
		self._routes: list[RouteSource] = []
		self._toolsets: list[ToolsetSource] = []
		self._auth: list[AuthProvider] = []
		self._auth_scopes: list[str] | None = None
		self._mcp_middleware: list[McpMiddleware] = []
		self._http_middleware: list[tuple[Callable[..., ASGIApp], dict[str, Any]]] = []
		self._lifespans: list[Lifespan] = []
		self._app: FastAPI | None = None

	def with_routes(self, *routes: RouteSource | list[RouteSource] | tuple[RouteSource, ...]) -> Self:
		"""
		Adds HTTP routes: ``Routes`` classes or instances, ``Route`` endpoints, or FastAPI ``APIRouter`` s.

		Lists of them are accepted too, e.g. a module's ``ROUTES = [Route(...), Route(...)]``.

		:param routes: Route sources, or lists of them.
		:type routes: RouteSource | list[RouteSource] | tuple[RouteSource, ...]
		:return: The builder.
		:rtype: Self
		"""
		for source in routes:
			if isinstance(source, list | tuple):
				self._routes.extend(source)
			else:
				self._routes.append(source)
		return self

	def with_toolsets(self, *toolsets: ToolsetSource | list[ToolsetSource] | tuple[ToolsetSource, ...]) -> Self:
		"""
		Adds MCP components: ``Toolset`` classes or instances, or function ``Tool`` s.

		Lists of them are accepted too, e.g. a module's ``TOOLS = [Tool(...), Tool(...)]``.

		:param toolsets: Toolset sources, or lists of them.
		:type toolsets: ToolsetSource | list[ToolsetSource] | tuple[ToolsetSource, ...]
		:return: The builder.
		:rtype: Self
		"""
		for source in toolsets:
			if isinstance(source, list | tuple):
				self._toolsets.extend(source)
			else:
				self._toolsets.append(source)
		return self

	def with_auth(self, *providers: AuthProvider | None, required_scopes: list[str] | None = None) -> Self:
		"""
		Protects the MCP endpoint. Without it, the endpoint is open.

		Takes any FastMCP auth provider: an OAuth provider (``GitHubProvider``, ``OIDCProxy``, ...),
		``JWTVerifier``, ``ApiKeyVerifier``, or ``auth_from_settings(settings)``. With several, a
		request is accepted if any of them accepts its token; at most one may be an OAuth
		provider. ``None`` entries are ignored, so a settings-driven "no auth" passes through.

		When combining an OAuth provider with other verifiers, every token must carry the OAuth
		provider's scopes unless ``required_scopes`` says otherwise (see ``combine_auth``).

		:param providers: Auth providers for the MCP endpoint.
		:type providers: AuthProvider | None
		:param required_scopes: Scopes every token must carry when combining providers.
		:type required_scopes: list[str] | None
		:return: The builder.
		:rtype: Self
		"""
		self._auth.extend(p for p in providers if p is not None)
		if required_scopes is not None:
			self._auth_scopes = required_scopes
		return self

	def with_lifespan(self, *lifespans: Lifespan) -> Self:
		"""
		Runs startup/shutdown code with the app, e.g. opening a database connection.

		Each is an async context manager factory taking the app (like FastAPI's ``lifespan``).
		They start in the order given, before the MCP server, and stop in reverse. Each worker
		process runs its own.

		:param lifespans: Lifespan context manager factories.
		:type lifespans: Lifespan
		:return: The builder.
		:rtype: Self
		"""
		self._lifespans.extend(lifespans)
		return self

	def with_mcp_middleware(self, *middleware: McpMiddleware) -> Self:
		"""
		Adds FastMCP middleware, which sees each MCP request with its method, tool/resource
		name and the caller's verified token (e.g. audit logging, per-tool authorization).

		Runs after auth, request logging and rate limiting, in the order given.

		:param middleware: FastMCP ``Middleware`` instances.
		:type middleware: Middleware
		:return: The builder.
		:rtype: Self
		"""
		self._mcp_middleware.extend(middleware)
		return self

	def with_http_middleware(self, middleware_class: Callable[..., ASGIApp], **options: Any) -> Self:
		"""
		Adds ASGI middleware around every HTTP route, MCP included, like ``app.add_middleware``.

		Runs inside the request-ID/access-log and CORS middleware, in the order added (first is
		outermost), and before MCP auth, so it sees unauthenticated requests too.

		:param middleware_class: ASGI middleware class, e.g. ``GZipMiddleware``.
		:type middleware_class: Callable[..., ASGIApp]
		:param options: Keyword arguments for the middleware.
		:return: The builder.
		:rtype: Self
		"""
		self._http_middleware.append((middleware_class, options))
		return self

	def build(self) -> FastAPI:
		settings = self.settings
		configure_logging(settings)

		auth = combine_auth(self._auth, self._auth_scopes)
		lifespans: list[Lifespan] = [self._lifespan, *self._lifespans]
		mcp_app = None
		if settings.mcp.enabled:
			factory = McpServerFactory(settings)
			mcp_server = factory.create(self._toolsets, auth=auth, middleware=self._mcp_middleware)
			mcp_app = factory.http_app(mcp_server)
			lifespans.append(mcp_app.lifespan)

		app = FastAPI(
			title=settings.name,
			version=settings.version,
			debug=settings.debug,
			root_path=settings.server.root_path,
			docs_url="/docs" if settings.show_docs else None,
			redoc_url="/redoc" if settings.show_docs else None,
			openapi_url="/openapi.json" if settings.show_docs else None,
			lifespan=combine_lifespans(*lifespans),
		)

		route_auth = [Depends(http_bearer_auth(auth))] if auth is not None else []
		for source in self._routes:
			requires_auth = getattr(source, "requires_auth", False) is True
			app.include_router(self._router(source), dependencies=route_auth if requires_auth else [])

		# add_middleware prepends, so the last one added is outermost:
		# RequestContextMiddleware -> CORS -> injected middleware (first added outermost) -> routes.
		for middleware_class, options in reversed(self._http_middleware):
			app.add_middleware(middleware_class, **options)
		if settings.cors.allow_origins:
			app.add_middleware(CORSMiddleware, **settings.cors.model_dump())
		app.add_middleware(
			RequestContextMiddleware,
			access_log=settings.logging.access_log,
			exclude_paths=settings.logging.access_log_exclude_paths,
		)

		if mcp_app is not None:
			# At the root so the endpoint lives at `mcp.path` and OAuth discovery routes
			# (/.well-known/...) resolve where clients expect them.
			app.router.routes.append(_RootMount(mcp_app))

		self._app = app
		return app

	def run(self, import_string: str = "main:app") -> None:
		"""
		Runs the application using Uvicorn. The function configures and starts the Uvicorn
		server instance based on the provided import string and server settings. It supports
		different configurations such as worker processes, reload settings, proxy headers,
		and timeouts. Logging for access lines and server header is disabled.

		:param import_string: The import path for the application, in the format
		                      "module_name:application_instance_name". Defaults to "main:app".
		:type import_string: str
		:return: This function does not return any value.
		:rtype: None
		"""
		server = self.settings.server
		needs_import = server.reload or server.workers > 1
		uvicorn.run(
			import_string if needs_import else (self._app or self.build()),
			host=server.host,
			port=server.port,
			workers=server.workers,
			reload=server.reload,
			proxy_headers=server.proxy_headers,
			forwarded_allow_ips=server.forwarded_allow_ips,
			timeout_keep_alive=server.timeout_keep_alive,
			timeout_graceful_shutdown=server.timeout_graceful_shutdown,
			limit_concurrency=server.limit_concurrency,
			# Logging is owned by configure_logging(); access lines come from RequestContextMiddleware.
			log_config=None,
			access_log=False,
			server_header=False,
		)

	def _router(self, source: RouteSource) -> APIRouter:
		if isinstance(source, APIRouter):
			return source
		if isinstance(source, Routes | Route):
			return source.router()
		if isinstance(source, type) and issubclass(source, Routes):
			return source(self.settings).router()
		raise TypeError(f"with_routes() expects a Routes class or instance, Route, or APIRouter; got {source!r}")

	@asynccontextmanager
	async def _lifespan(self, app: FastAPI) -> AsyncIterator[None]:
		"""
		Handles the lifespan context of the FastAPI application. This method is an async
		context manager which is invoked during the startup and shutdown of the app.
		Initializes logging information about the application and its settings.

		:param app: The FastAPI application instance.
		:type app: FastAPI
		:return: Yields control for the application's lifespan and handles shutdown logging.
		:rtype: AsyncIterator[None]
		"""
		settings = self.settings
		mcp = settings.mcp
		log.info(
			"app.startup",
			name=settings.name,
			version=settings.version,
			environment=settings.environment,
			routes=[_describe(r) for r in self._routes],
			http_middleware=[_describe(m) for m, _ in self._http_middleware],
			mcp_enabled=mcp.enabled,
			mcp_path=mcp.path if mcp.enabled else None,
			mcp_auth=[_describe(p) for p in self._auth],
			mcp_middleware=[_describe(m) for m in self._mcp_middleware],
			mcp_stateless=mcp.stateless_http if mcp.enabled else None,
			toolsets=[_describe(t) for t in self._toolsets],
		)
		if settings.is_production and mcp.enabled and not self._auth:
			log.warning("mcp.auth.disabled", detail="MCP endpoint is unauthenticated; ensure a gateway enforces auth")
		yield
		log.info("app.shutdown")


def _describe(source: object) -> str:
	if isinstance(source, APIRouter):
		return f"APIRouter({source.prefix or '/'})"
	if isinstance(source, Route | Tool):
		return repr(source)
	return source.__name__ if isinstance(source, type) else type(source).__name__
