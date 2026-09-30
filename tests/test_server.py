from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, SecretStr

from app.core.auth import ApiKeyVerifier
from app.core.settings import Settings
from app.routes import Route, Routes, get
from app.server import ServerBuilder
from app.tools import Toolset, mcp_tool
from tests.conftest import MCP_HEADERS, make_settings


class FileStore:
	def __init__(self) -> None:
		self.files: dict[str, str] = {}


class FileTools(Toolset):
	def __init__(self, settings: Settings, files: FileStore) -> None:
		super().__init__(settings)
		self.files = files

	@mcp_tool()
	def export_notes(self, text: str) -> str:
		"""Save text as a file and return its download link."""
		file_id = str(len(self.files.files))
		self.files.files[file_id] = text
		return f"/files/{file_id}"


class FileRoutes(Routes):
	prefix = "/files"

	def __init__(self, settings: Settings, files: FileStore) -> None:
		super().__init__(settings)
		self.files = files

	@get("/{file_id}")
	async def download(self, file_id: str) -> PlainTextResponse:
		if file_id not in self.files.files:
			raise HTTPException(status_code=404)
		return PlainTextResponse(
			self.files.files[file_id],
			headers={"Content-Disposition": f'attachment; filename="notes-{file_id}.txt"'},
		)


def test_injected_instances_share_dependencies(make_client):
	settings = make_settings(mcp={"stateless_http": True, "json_response": True})
	files = FileStore()
	client = make_client(settings, routes=(FileRoutes(settings, files),), toolsets=(FileTools(settings, files),))

	call = {
		"jsonrpc": "2.0",
		"id": 1,
		"method": "tools/call",
		"params": {"name": "export_notes", "arguments": {"text": "hello file"}},
	}
	link = client.post("/mcp", headers=MCP_HEADERS, json=call).json()["result"]["structuredContent"]["result"]
	assert link == "/files/0"

	download = client.get(link)
	assert download.status_code == 200
	assert download.text == "hello file"
	assert download.headers["content-disposition"] == 'attachment; filename="notes-0.txt"'


def test_plain_api_router(make_client):
	router = APIRouter(prefix="/plain")

	@router.get("/ping")
	async def ping() -> dict[str, bool]:
		return {"pong": True}

	client = make_client(routes=(router,))
	assert client.get("/plain/ping").json() == {"pong": True}


class Order(BaseModel):
	item: str
	quantity: int = 1


def _tenant(x_tenant: Annotated[str, Header()]) -> str:
	return x_tenant


async def get_order(
	order_id: int, tenant: Annotated[str, Depends(_tenant)], verbose: Annotated[bool, Query()] = False
) -> dict:
	return {"order_id": order_id, "tenant": tenant, "verbose": verbose}


def create_order(order: Order) -> Order:
	return order


def test_function_routes_get_fastapi_parameters_and_depends(make_client):
	audited: list[str] = []

	def audit(tenant: Annotated[str, Depends(_tenant)]) -> None:
		audited.append(tenant)

	client = make_client(
		routes=(
			Route("/orders/{order_id}", get_order, "GET", summary="Get an order"),
			Route("/orders", create_order, "post", status_code=201, dependencies=[Depends(audit)]),
		)
	)
	fetched = client.get("/orders/7?verbose=true", headers={"X-Tenant": "acme"})
	assert fetched.json() == {"order_id": 7, "tenant": "acme", "verbose": True}
	assert client.get("/orders/7").status_code == 422
	assert client.get("/orders/not-a-number", headers={"X-Tenant": "acme"}).status_code == 422

	created = client.post("/orders", json={"item": "widget"}, headers={"X-Tenant": "acme"})
	assert created.status_code == 201
	assert created.json() == {"item": "widget", "quantity": 1}
	assert audited == ["acme"]
	assert client.post("/orders", json={"quantity": 2}, headers={"X-Tenant": "acme"}).status_code == 422
	assert client.get("/orders").status_code == 405

	assert client.get("/openapi.json").json()["paths"]["/orders/{order_id}"]["get"]["summary"] == "Get an order"


def test_function_routes_can_require_mcp_auth(make_client):
	async def ping() -> dict[str, bool]:
		return {"pong": True}

	client = make_client(
		routes=(Route("/private", ping, requires_auth=True), Route("/public", ping)),
		auth=(ApiKeyVerifier([SecretStr("k1")]),),
	)
	assert client.get("/private").status_code == 401
	assert client.get("/private", headers={"Authorization": "Bearer k1"}).json() == {"pong": True}
	assert client.get("/public").status_code == 200


def test_with_routes_accepts_lists_and_several_methods(make_client):
	def echo(value: str = "x") -> dict[str, str]:
		return {"value": value}

	client = make_client(routes=([Route("/echo", echo, ["GET", "PUT"]), Route("/echo2", echo)],))
	assert client.get("/echo?value=a").json() == {"value": "a"}
	assert client.put("/echo").json() == {"value": "x"}
	assert client.get("/echo2").status_code == 200


def test_route_needs_a_method():
	with pytest.raises(ValueError, match="at least one HTTP method"):
		Route("/nothing", create_order, [])
	assert repr(Route("/orders", create_order, "post")) == "Route(POST /orders -> create_order)"


def test_rejects_unknown_route_and_toolset_sources():
	with pytest.raises(TypeError, match="with_routes"):
		ServerBuilder(make_settings()).with_routes(object()).build()  # pyright: ignore[reportArgumentType]
	with pytest.raises(TypeError, match="Toolset"):
		ServerBuilder(make_settings()).with_toolsets(object()).build()  # pyright: ignore[reportArgumentType]
