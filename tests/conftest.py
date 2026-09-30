from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import Middleware
from pydantic_settings import SettingsConfigDict

from app.core.auth import auth_from_settings
from app.core.settings import Environment, Settings
from app.routes import HealthRoutes, HelloRoutes, RouteSource
from app.server import ServerBuilder
from app.tools import GreetingTools, ToolsetSource

MCP_HEADERS = {
	"Content-Type": "application/json",
	"Accept": "application/json, text/event-stream",
	"Mcp-Protocol-Version": "2025-06-18",
}

TOOL_CALL = {
	"jsonrpc": "2.0",
	"id": 1,
	"method": "tools/call",
	"params": {"name": "greet", "arguments": {"name": "Joe"}},
}


class IsolatedSettings(Settings):
	"""Settings that ignore a developer's local .env file."""

	model_config = SettingsConfigDict(env_file=None)


def make_settings(**overrides: Any) -> Settings:
	return IsolatedSettings(**{"environment": Environment.TEST, **overrides})


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
	clients: list[TestClient] = []

	def factory(
		settings: Settings | None = None,
		routes: tuple[RouteSource, ...] = (),
		toolsets: tuple[ToolsetSource, ...] = (),
		auth: tuple[AuthProvider, ...] | None = None,
		auth_scopes: list[str] | None = None,
		mcp_middleware: tuple[Middleware, ...] = (),
	) -> TestClient:
		settings = settings or make_settings()
		app = (
			ServerBuilder(settings)
			.with_auth(*(auth if auth is not None else (auth_from_settings(settings),)), required_scopes=auth_scopes)
			.with_mcp_middleware(*mcp_middleware)
			.with_routes(HealthRoutes, HelloRoutes, *routes)
			.with_toolsets(GreetingTools, *toolsets)
			.build()
		)
		client = TestClient(app, raise_server_exceptions=False)
		client.__enter__()
		clients.append(client)
		return client

	yield factory
	for client in clients:
		client.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
	return make_client()
