import base64
import hashlib
import json
import re
import secrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth import MultiAuth, OIDCProxy
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from joserfc.jwk import RSAKey
from key_value.aio.stores.memory import MemoryStore
from pydantic import SecretStr
from pymongo import MongoClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth import AllowedUsers, ApiKeyVerifier, auth_from_settings, combine_auth
from app.core.settings import Settings
from app.server import ServerBuilder
from app.tools import IdentityTools
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


# --- OAuth sign-in (mcp.auth.mode=oauth) --------------------------------------

BASE_URL = "http://localhost:8000"
REDIRECT_URI = "http://localhost:3000/callback"
WHOAMI = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}


class FakeIdp:
	"""A minimal OIDC provider on localhost: discovery, JWKS, and a token endpoint that signs in ``claims``."""

	def __init__(self) -> None:
		self.keys = RSAKeyPair.generate()
		self.claims: dict[str, Any] = {"email": "ada@example.com", "email_verified": True}
		self.issued: list[str] = []
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
		self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

	def _handler(self) -> type[BaseHTTPRequestHandler]:
		idp = self

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self) -> None:
				if self.path == "/.well-known/openid-configuration":
					self._json(
						{
							"issuer": idp.url,
							"authorization_endpoint": f"{idp.url}/authorize",
							"token_endpoint": f"{idp.url}/token",
							"jwks_uri": f"{idp.url}/jwks",
							"response_types_supported": ["code"],
							"subject_types_supported": ["public"],
							"id_token_signing_alg_values_supported": ["RS256"],
						}
					)
				elif self.path == "/jwks":
					key = RSAKey.import_key(idp.keys.public_key, {"kid": "k1", "use": "sig", "alg": "RS256"})
					self._json({"keys": [key.as_dict()]})
				else:
					self.send_error(404)

			def do_POST(self) -> None:  # the token endpoint, whatever code it's given
				self.rfile.read(int(self.headers["Content-Length"]))
				token = idp.keys.create_token(
					subject="user-1", issuer=idp.url, scopes=["openid", "email"], additional_claims=idp.claims, kid="k1"
				)
				idp.issued.append(token)
				self._json({"access_token": token, "token_type": "Bearer", "expires_in": 3600, "scope": "openid email"})

			def _json(self, body: dict) -> None:
				data = json.dumps(body).encode()
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(data)))
				self.end_headers()
				self.wfile.write(data)

			def log_message(self, format: str, *args: object) -> None:
				pass

		return Handler


@pytest.fixture
def idp() -> Iterator[FakeIdp]:
	idp = FakeIdp()
	threading.Thread(target=idp.server.serve_forever, daemon=True).start()
	yield idp
	idp.server.shutdown()
	idp.server.server_close()


def _oauth_settings(idp: FakeIdp, **overrides: Any) -> Settings:
	auth = {
		"mode": "oauth",
		"provider": "oidc",
		"config_url": f"{idp.url}/.well-known/openid-configuration",
		"client_id": "app-id",
		"client_secret": "app-secret",
		"base_url": BASE_URL,
		"storage": "memory",
	}
	return make_settings(mcp={**STATELESS, "auth": {**auth, **overrides.pop("auth", {})}}, **overrides)


@contextmanager
def _oauth_client(settings: Settings) -> Iterator[TestClient]:
	app = ServerBuilder(settings).with_auth(auth_from_settings(settings)).with_toolsets(IdentityTools).build()
	# The client plays the browser too, so it must use the host in base_url for the consent cookie.
	with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
		yield client


def _sign_in(client: TestClient, idp: FakeIdp) -> str:
	"""Signs in the way an MCP client does, returning the access token this server issues."""
	registration = {"redirect_uris": [REDIRECT_URI], "token_endpoint_auth_method": "none"}
	client_id = client.post("/register", json=registration).json()["client_id"]
	verifier = secrets.token_urlsafe(48)
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
	authorize = client.get(
		"/authorize",
		params={
			"response_type": "code",
			"client_id": client_id,
			"redirect_uri": REDIRECT_URI,
			"code_challenge": challenge,
			"code_challenge_method": "S256",
			"state": "client-state",
		},
	)
	consent = client.get(authorize.headers["location"])
	form = dict(re.findall(r'name="(txn_id|csrf_token)" value="([^"]*)"', consent.text))
	upstream = urlsplit(client.post("/consent", data={**form, "action": "approve"}).headers["location"])

	# The browser is sent to the IdP with this server's app registration...
	assert f"{upstream.scheme}://{upstream.netloc}{upstream.path}" == f"{idp.url}/authorize"
	query = parse_qs(upstream.query)
	assert query["client_id"] == ["app-id"]
	assert query["redirect_uri"] == [f"{BASE_URL}/auth/callback"]

	# ...signs in, and comes back here; this server exchanges the IdP's code, then hands the client its own.
	callback = client.get("/auth/callback", params={"code": "idp-code", "state": query["state"][0]})
	back = parse_qs(urlsplit(callback.headers["location"]).query)
	assert back["state"] == ["client-state"]
	token = client.post(
		"/token",
		data={
			"grant_type": "authorization_code",
			"code": back["code"][0],
			"redirect_uri": REDIRECT_URI,
			"client_id": client_id,
			"code_verifier": verifier,
		},
	)
	assert token.status_code == 200, token.text
	return token.json()["access_token"]


def _whoami(client: TestClient, token: str):
	return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json=WHOAMI)


def test_oauth_sign_in(idp):
	with _oauth_client(_oauth_settings(idp)) as client:
		unauthenticated = client.post("/mcp", headers=MCP_HEADERS, json=WHOAMI)
		assert unauthenticated.status_code == 401
		challenge = unauthenticated.headers["www-authenticate"]
		assert f'resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource/mcp"' in challenge
		metadata = client.get("/.well-known/oauth-authorization-server").json()
		assert metadata["registration_endpoint"] == f"{BASE_URL}/register"

		response = _whoami(client, _sign_in(client, idp))
		assert response.status_code == 200
		assert response.json()["result"]["structuredContent"]["subject"] == "user-1"
		# The IdP's own token isn't accepted; clients only ever hold tokens this server issued.
		assert _whoami(client, idp.issued[-1]).status_code == 401


@pytest.mark.parametrize(
	("claims", "allowed"),
	[
		({"email": "ada@example.com", "email_verified": True}, True),
		({"email": "grace@elsewhere.org", "email_verified": True}, True),
		({"email": "eve@evil.test", "email_verified": True}, False),
		({"email": "eve@example.com", "email_verified": False}, False),
		({"email": "eve@example.com"}, False),
	],
)
def test_oauth_allowed_users(idp, claims, allowed):
	idp.claims = claims
	settings = _oauth_settings(idp, auth={"allowed_users": ["Grace@Elsewhere.org"], "allowed_domains": ["example.com"]})
	with _oauth_client(settings) as client:
		assert _whoami(client, _sign_in(client, idp)).status_code == (200 if allowed else 401)


@pytest.mark.parametrize(
	("claims", "allowed"),
	[
		({"login": "OctoCat", "email": None}, True),
		# GitHub only publishes verified emails, and sends no email_verified.
		({"login": "someone", "email": "octo@github.com"}, True),
		({"email": "octo@github.com", "email_verified": "true"}, True),
		({"email": "octo@github.com", "email_verified": "false"}, False),
		({"email": "ada@example.com", "email_verified": True}, True),
		({"email": "ada@eng.example.com", "email_verified": True}, False),
		({"email": "example.com", "email_verified": True}, False),
		({"login": "someone-else", "email": None}, False),
		({}, False),
	],
)
def test_allowed_users_matching(claims, allowed):
	allow = AllowedUsers(_api_keys("k"), users=["octocat", "octo@github.com", " "], domains=["@Example.com"])
	assert allow.allows(claims) is allowed


def test_oauth_providers_from_settings(idp):
	github = auth_from_settings(_oauth_settings(idp, auth={"provider": "github"}))
	assert isinstance(github, GitHubProvider)

	google = auth_from_settings(_oauth_settings(idp, auth={"provider": "google"}))
	assert isinstance(google, GoogleProvider)
	assert google.required_scopes == ["openid", "https://www.googleapis.com/auth/userinfo.email"]

	assert isinstance(auth_from_settings(_oauth_settings(idp)), OIDCProxy)

	limited = auth_from_settings(_oauth_settings(idp, auth={"provider": "github", "allowed_users": ["octocat"]}))
	assert isinstance(limited, AllowedUsers)
	assert isinstance(limited.server, GitHubProvider)


def test_oauth_state_is_shared_and_encrypted_in_mongo(idp, mongo_url):
	database = f"test_{uuid.uuid4().hex[:12]}"

	def settings(signing_key: str) -> Settings:
		return _oauth_settings(
			idp,
			auth={"storage": "mongo", "jwt_signing_key": signing_key},
			mongo={"url": mongo_url, "database": database},
		)

	try:
		with _oauth_client(settings("signing-key-one")) as client:
			token = _sign_in(client, idp)
		# Another replica, or this one after a restart, accepts the token.
		with _oauth_client(settings("signing-key-one")) as client:
			assert _whoami(client, token).status_code == 200
		# Changing the signing key signs everyone out.
		with _oauth_client(settings("signing-key-two")) as client:
			assert _whoami(client, token).status_code == 401

		with MongoClient(mongo_url) as mongo:
			db = mongo[database]
			assert {"mcp-oauth-proxy-clients", "mcp-upstream-tokens"} <= set(db.list_collection_names())
			# Upstream tokens are credentials for people's IdP accounts; they're stored encrypted.
			assert idp.issued[-1] not in str(list(db["mcp-upstream-tokens"].find()))
	finally:
		with MongoClient(mongo_url) as mongo:
			mongo.drop_database(database)
