import json

import pytest
from fastmcp import Client, Context
from fastmcp.dependencies import CurrentContext, Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from mcp.types import TextContent, TextResourceContents, ToolAnnotations

from app.core.mcp import McpServerFactory
from app.tools import GreetingTools, Tool
from tests.conftest import MCP_HEADERS, TOOL_CALL, make_settings


@pytest.fixture
def mcp_server():
	return McpServerFactory(make_settings()).create([GreetingTools])


async def test_toolset_registers_components(mcp_server):
	async with Client(mcp_server) as client:
		assert [tool.name for tool in await client.list_tools()] == ["greet"]
		assert [str(r.uri) for r in await client.list_resources()] == ["server://info"]
		assert [prompt.name for prompt in await client.list_prompts()] == ["welcome"]


async def test_greet(mcp_server):
	async with Client(mcp_server) as client:
		result = await client.call_tool("greet", {"name": "Joe"})
		assert result.structured_content == {"message": "Hello Joe"}

		with pytest.raises(ToolError, match="name must not be empty"):
			await client.call_tool("greet", {"name": " "})


async def test_resource_and_prompt(mcp_server):
	async with Client(mcp_server) as client:
		[resource] = await client.read_resource("server://info")
		assert isinstance(resource, TextResourceContents)
		assert json.loads(resource.text)["environment"] == "test"

		prompt = await client.get_prompt("welcome", {"name": "Joe"})
		content = prompt.messages[0].content
		assert isinstance(content, TextContent)
		assert "Joe" in content.text


async def test_namespace_prefixes_components():
	class Namespaced(GreetingTools):
		namespace = "demo"

	server = McpServerFactory(make_settings()).create([Namespaced])
	async with Client(server) as client:
		assert [tool.name for tool in await client.list_tools()] == ["demo_greet"]


def _orders_db() -> dict[str, str]:
	return {"A1": "widget"}


async def lookup_order(order_id: str, db: dict = Depends(_orders_db), ctx: Context = CurrentContext()) -> str:
	"""Look up an order by ID."""
	if order_id not in db:
		raise ToolError(f"No order {order_id}")
	return f"{db[order_id]} ({type(ctx).__name__})"


def add(a: int, b: int) -> int:
	"""Add two numbers."""
	return a + b


def admin_report() -> str:
	return "secret"


async def test_function_tools_register_with_toolsets():
	server = McpServerFactory(make_settings()).create(
		[GreetingTools, Tool(lookup_order), Tool(add, name="sum", annotations=ToolAnnotations(read_only_hint=True))]
	)
	async with Client(server) as client:
		tools = {tool.name: tool for tool in await client.list_tools()}
		assert set(tools) == {"greet", "lookup_order", "sum"}
		assert tools["lookup_order"].description == "Look up an order by ID."
		# Injected parameters stay out of the schema the model sees.
		assert list(tools["lookup_order"].input_schema["properties"]) == ["order_id"]
		annotations = tools["sum"].annotations
		assert annotations is not None
		assert annotations.read_only_hint is True

		assert (await client.call_tool("lookup_order", {"order_id": "A1"})).data == "widget (Context)"
		assert (await client.call_tool("sum", {"a": 2, "b": 3})).data == 5
		with pytest.raises(ToolError, match="No order B2"):
			await client.call_tool("lookup_order", {"order_id": "B2"})


def test_function_tools_can_check_scopes(make_client):
	keys = RSAKeyPair.generate()
	settings = _stateless(auth={"mode": "jwt", "public_key": keys.public_key, "audience": "template-mcp"})
	client = make_client(settings, toolsets=([Tool(admin_report, auth=require_scopes("admin"))],))

	def call(scopes: list[str]) -> dict:
		token = keys.create_token(audience="template-mcp", scopes=scopes)
		body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "admin_report", "arguments": {}}}
		return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json=body).json()

	assert call(["admin"])["result"]["structuredContent"] == {"result": "secret"}
	denied = call([])["result"]
	assert denied["isError"]
	assert "Unknown tool" in denied["content"][0]["text"]


def test_tool_repr_names_the_tool():
	assert repr(Tool(add)) == "Tool(add)"
	assert repr(Tool(add, name="sum")) == "Tool(sum)"


def test_stateful_http_session(client):
	init = client.post(
		"/mcp",
		headers=MCP_HEADERS,
		json={
			"jsonrpc": "2.0",
			"id": 1,
			"method": "initialize",
			"params": {
				"protocolVersion": "2025-06-18",
				"capabilities": {},
				"clientInfo": {"name": "test", "version": "1"},
			},
		},
	)
	assert init.status_code == 200
	assert init.headers["mcp-session-id"]
	assert '"serverInfo":{"name":"template-mcp"' in init.text


def _stateless(**mcp):
	return make_settings(mcp={"stateless_http": True, "json_response": True, **mcp})


def test_stateless_json_tool_call(make_client):
	client = make_client(_stateless())
	response = client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL)
	assert response.status_code == 200
	assert response.json()["result"]["structuredContent"] == {"message": "Hello Joe"}


def test_api_key_auth(make_client):
	client = make_client(_stateless(auth={"mode": "static", "tokens": ["key-1", "key-2"]}))
	assert client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL).status_code == 401
	bad = {**MCP_HEADERS, "Authorization": "Bearer nope"}
	assert client.post("/mcp", headers=bad, json=TOOL_CALL).status_code == 401
	good = {**MCP_HEADERS, "Authorization": "Bearer key-2"}
	assert client.post("/mcp", headers=good, json=TOOL_CALL).status_code == 200
	# API routes are unaffected by MCP auth.
	assert client.get("/health/live").status_code == 200


def test_jwt_auth(make_client):
	keys = RSAKeyPair.generate()
	client = make_client(
		_stateless(
			auth={
				"mode": "jwt",
				"public_key": keys.public_key,
				"issuer": "https://idp.example.com",
				"audience": "template-mcp",
				"required_scopes": ["mcp:use"],
				"authorization_servers": ["https://idp.example.com"],
				"base_url": "https://mcp.example.com",
			}
		)
	)

	def call(**claims):
		token = keys.create_token(issuer="https://idp.example.com", **claims)
		return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"}, json=TOOL_CALL)

	assert call(audience="template-mcp", scopes=["mcp:use"]).status_code == 200
	assert call(audience="someone-else", scopes=["mcp:use"]).status_code == 401
	assert call(audience="template-mcp", scopes=["other"]).status_code == 401

	metadata = client.get("/.well-known/oauth-protected-resource/mcp")
	assert metadata.status_code == 200
	assert metadata.json()["resource"] == "https://mcp.example.com/mcp"
	assert metadata.json()["authorization_servers"] == ["https://idp.example.com"]


def test_rate_limit_is_per_client(make_client):
	client = make_client(
		_stateless(
			auth={"mode": "static", "tokens": ["key-a", "key-b"]},
			rate_limit={"enabled": True, "requests_per_second": 0.001, "burst_capacity": 2},
		)
	)

	def call(key: str) -> dict:
		headers = {**MCP_HEADERS, "Authorization": f"Bearer {key}"}
		return client.post("/mcp", headers=headers, json=TOOL_CALL).json()

	assert "result" in call("key-a")
	assert "result" in call("key-a")
	assert "rate limit" in call("key-a")["error"]["message"].lower()
	# A different client has its own bucket.
	assert "result" in call("key-b")


def test_mcp_can_be_disabled(make_client):
	client = make_client(make_settings(mcp={"enabled": False}))
	assert client.post("/mcp", headers=MCP_HEADERS, json=TOOL_CALL).status_code == 404
