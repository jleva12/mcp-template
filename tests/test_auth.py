import pytest
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from key_value.aio.stores.memory import MemoryStore
from pydantic import SecretStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth import ApiKeyVerifier, combine_auth
from app.server import ServerBuilder
from tests.conftest import MCP_HEADERS, TOOL_CALL, make_settings

STATELESS = {"stateless_http": True, "json_response": True}


def _github() -> GitHubProvider:
	return GitHubProvider(
		client_id="gh-client",
		client_secret="gh-secret",
		base_url="http://localhost:8000",  # the MCP SDK allows non-HTTPS issuers only on localhost
		client_storage=MemoryStore(),
		jwt_signing_key="test-signing-key",
	)


def _api_keys(*keys: str) -> ApiKeyVerifier:
	return ApiKeyVerifier([SecretStr(k) for k in keys])


def test_combine_auth():
	keys = _api_keys("k")
	assert combine_auth([]) is None
	assert combine_auth([keys]) is keys
	assert isinstance(combine_auth([_github(), keys]), MultiAuth)
	with pytest.raises(ValueError, match="only one auth provider"):
		combine_auth([_github(), _github()])


def test_no_auth_leaves_mcp_open(make_client):
	client = make_client(make_settings(mcp=STATELESS), auth=())
	assert client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL).status_code == 200


def test_oauth_and_api_keys_together(make_client):
	"""People sign in with GitHub OAuth; services use API keys on the same endpoint."""
	client = make_client(make_settings(mcp=STATELESS), auth=(_github(), _api_keys("service-key")), auth_scopes=[])

	unauthenticated = client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL)
	assert unauthenticated.status_code == 401
	assert "resource_metadata=" in unauthenticated.headers["www-authenticate"]

	service = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer service-key"}, json=TOOL_CALL)
	assert service.status_code == 200

	# OAuth discovery and flow endpoints are served at the root, where clients look for them.
	metadata = client.get("/.well-known/oauth-authorization-server").json()
	assert metadata["authorization_endpoint"] == "http://localhost:8000/authorize"
	assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
	assert client.get("/authorize").status_code != 404


def test_oauth_and_jwt_together(make_client):
	keys = RSAKeyPair.generate()
	jwt = JWTVerifier(public_key=keys.public_key, issuer="https://idp.example.com", audience="template-mcp")
	client = make_client(make_settings(mcp=STATELESS), auth=(_github(), jwt), auth_scopes=[])
	token = keys.create_token(issuer="https://idp.example.com", audience="template-mcp")
	response = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json=TOOL_CALL)
	assert response.status_code == 200


def test_combined_auth_requires_oauth_scopes_by_default(make_client):
	# GitHubProvider requires the "user" scope; an API key doesn't carry it.
	client = make_client(make_settings(mcp=STATELESS), auth=(_github(), _api_keys("service-key")))
	response = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer service-key"}, json=TOOL_CALL)
	assert response.status_code == 403


class RecordingMiddleware(Middleware):
	def __init__(self) -> None:
		self.calls: list[str] = []

	async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
		self.calls.append(context.message.name)
		return await call_next(context)


def test_injected_mcp_middleware_sees_tool_calls(make_client):
	recorder = RecordingMiddleware()
	client = make_client(make_settings(mcp=STATELESS), mcp_middleware=(recorder,))
	client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL)
	assert recorder.calls == ["greet"]


class HeaderMiddleware:
	def __init__(self, app: ASGIApp, *, name: str, value: str) -> None:
		self.app = app
		self.header = (name.lower().encode(), value.encode())

	async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
		async def send_with_header(message: Message) -> None:
			if message["type"] == "http.response.start":
				message["headers"] = [*message.get("headers", []), self.header]
			await send(message)

		await self.app(scope, receive, send_with_header)


def test_injected_http_middleware_wraps_api_and_mcp_routes():
	settings = make_settings(mcp=STATELESS)
	app = (
		ServerBuilder(settings)
		.with_http_middleware(HeaderMiddleware, name="X-Served-By", value="template")
		.with_toolsets()
		.build()
	)
	from fastapi.testclient import TestClient

	with TestClient(app) as client:
		for response in (client.get("/"), client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL)):
			assert response.headers["x-served-by"] == "template"
			assert response.headers["x-request-id"]
