"""The template studio MCP App against a real MongoDB (see tests/test_templates.py)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.core.database import Database
from app.core.files import FileStore
from app.models import MODELS
from app.server import ServerBuilder
from app.services.rendering import DocxRenderer, PdfRenderer, RendererRegistry
from app.services.templates import TemplateService
from app.tools import TemplateStudioTools, TemplateTools
from app.tools.template_studio import STUDIO_URI
from tests.conftest import MCP_HEADERS, make_settings
from tests.test_templates import DATA, INVOICE, SCHEMA

UI_MIME_TYPE = "text/html;profile=mcp-app"


@pytest.fixture
def client(mongo_url, tmp_path):
	database_name = f"test_{uuid.uuid4().hex[:12]}"
	settings = make_settings(
		mcp={"stateless_http": True, "json_response": True},
		mongo={"url": mongo_url, "database": database_name},
		files={"signing_key": "test-key", "local_dir": str(tmp_path)},
	)
	database = Database(settings, MODELS)
	service = TemplateService(
		settings, RendererRegistry(PdfRenderer(settings), DocxRenderer(settings)), FileStore.from_settings(settings)
	)
	app = (
		ServerBuilder(settings)
		.with_lifespan(database.lifespan)
		.with_toolsets(TemplateTools(settings, service), TemplateStudioTools(settings, service))
		.build()
	)
	with TestClient(app) as test_client:
		yield test_client
	MongoClient(mongo_url).drop_database(database_name)


def _rpc(client, method: str, **params) -> dict:
	body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
	return client.post("/mcp", headers=MCP_HEADERS, json=body).json()["result"]


def _call(client, tool: str, **arguments) -> dict:
	return _rpc(client, "tools/call", name=tool, arguments=arguments)


def _create(client, content=INVOICE, schema: dict | None = SCHEMA) -> str:
	created = _call(client, "create_template", name="Invoice", content=content, json_schema=schema)
	return created["structuredContent"]["template_id"]


def test_tools_link_to_the_studio_ui(client):
	tools = {tool["name"]: tool for tool in _rpc(client, "tools/list")["tools"]}

	assert tools["open_template_studio"]["_meta"]["ui"] == {"resourceUri": STUDIO_URI}
	# App-only: listed (hosts do the filtering), but marked so clients keep it from the model.
	assert tools["preview_template"]["_meta"]["ui"] == {"resourceUri": STUDIO_URI, "visibility": ["app"]}
	assert "ui" not in (tools["render_document"].get("_meta") or {})


def test_studio_resource_is_an_mcp_app(client):
	resources = {r["uri"]: r for r in _rpc(client, "resources/list")["resources"]}
	assert resources[STUDIO_URI]["mimeType"] == UI_MIME_TYPE
	assert resources[STUDIO_URI]["_meta"]["ui"] == {"prefersBorder": True}

	[contents] = _rpc(client, "resources/read", uri=STUDIO_URI)["contents"]
	assert contents["mimeType"] == UI_MIME_TYPE
	assert contents["text"].startswith("<!doctype html>")
	# The page talks to the host with the MCP Apps protocol and calls these tools.
	for marker in ('"ui/initialize"', '"preview_template"', '"render_document"'):
		assert marker in contents["text"]


def test_open_template_studio(client):
	template_id = _create(client)
	_call(client, "add_template_version", template_id=template_id, content=INVOICE.replace("Invoice", "Bill"))

	opened = _call(client, "open_template_studio", template_id=template_id, version=1, template_data=DATA)
	assert not opened["isError"]
	# The model gets a one-line summary; the page gets the template in structuredContent.
	assert opened["content"] == [{"type": "text", "text": "Opened the template studio for 'Invoice' (version 1)."}]
	session = opened["structuredContent"]
	assert session["template"]["latest_version"] == 2
	assert [v["version"] for v in session["template"]["versions"]] == [1, 2]
	assert session["source"]["version"] == 1
	assert session["source"]["content"] == INVOICE
	assert session["source"]["json_schema"] == SCHEMA
	assert session["template_data"] == DATA

	latest = _call(client, "open_template_studio", template_id=template_id)["structuredContent"]
	assert latest["source"]["version"] == 2
	assert latest["template_data"] is None

	missing = _call(client, "open_template_studio", template_id="tpl_missing")
	assert missing["isError"]
	assert "not found" in missing["content"][0]["text"]


def test_preview_fills_the_template_as_html(client):
	template_id = _create(client)
	data = {**DATA, "customer": {"name": "<Acme & Co>"}}

	preview = _call(client, "preview_template", template_id=template_id, template_data=data)["structuredContent"]
	assert preview["template_version"] == 1
	assert "<h1>Invoice INV-1</h1>" in preview["html"]
	assert "<p>&lt;Acme &amp; Co&gt;</p>" in preview["html"]
	assert "Widget: 9.5" in preview["html"]


def test_preview_reports_invalid_data(client):
	template_id = _create(client)
	invalid = _call(client, "preview_template", template_id=template_id, template_data={"number": "x"})
	assert invalid["isError"]
	assert "(root): 'customer' is a required property" in invalid["content"][0]["text"]

	without_schema = _create(client, schema=None)
	missing = _call(client, "preview_template", template_id=without_schema, template_data={})
	assert missing["isError"]
	assert "'number' is undefined" in missing["content"][0]["text"]
