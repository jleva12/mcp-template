"""Templates and rendering against a real MongoDB (APP_TEST_MONGO_URL, default localhost).

Start one with `docker compose up -d mongo`. PDF rendering needs WeasyPrint's system libraries;
on macOS run pytest with DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib.
"""

import html
import io
import json
import re
import threading
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import MongoClient

from app.core.auth import ApiKeyVerifier
from app.core.database import Database
from app.core.files import FileStore
from app.models import MODELS
from app.routes import DocumentRoutes, FileRoutes, HealthRoutes, TemplateRoutes
from app.server import ServerBuilder
from app.services.rendering import DocxRenderer, PdfRenderer, RendererRegistry
from app.services.templates import TemplateService
from app.tools import TemplateTools
from tests.conftest import MCP_HEADERS, make_settings

INVOICE = """<!doctype html><html><head><style>@page { size: A4; margin: 2cm }</style></head><body>
<h1>Invoice {{ number }}</h1><p>{{ customer.name }}</p>
{% for item in items %}<p>{{ item.description }}: {{ item.price }}</p>{% endfor %}
{% if notes is defined %}<p>{{ notes }}</p>{% endif %}
</body></html>"""

SCHEMA = {
	"type": "object",
	"required": ["number", "customer", "items"],
	"properties": {
		"number": {"type": "string"},
		"customer": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
		"items": {
			"type": "array",
			"items": {
				"type": "object",
				"required": ["description", "price"],
				"properties": {"price": {"type": "number"}},
			},
		},
	},
}

DATA = {"number": "INV-1", "customer": {"name": "Acme"}, "items": [{"description": "Widget", "price": 9.5}]}

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _build_client(mongo_url: str, tmp_path, auth: tuple = ()):
	database_name = f"test_{uuid.uuid4().hex[:12]}"
	settings = make_settings(
		mcp={"stateless_http": True, "json_response": True},
		mongo={"url": mongo_url, "database": database_name},
		files={"signing_key": "test-key", "local_dir": str(tmp_path)},
	)
	database = Database(settings, MODELS)
	files = FileStore.from_settings(settings)
	service = TemplateService(settings, RendererRegistry(PdfRenderer(settings), DocxRenderer(settings)), files)
	app = (
		ServerBuilder(settings)
		.with_auth(*auth)
		.with_lifespan(database.lifespan)
		.with_routes(
			HealthRoutes(settings, checks={"mongo": database.ping}),
			FileRoutes(settings, files),
			TemplateRoutes(settings, service),
			DocumentRoutes(settings, service),
		)
		.with_toolsets(TemplateTools(settings, service))
		.build()
	)
	return app, database_name


@pytest.fixture
def client(mongo_url, tmp_path):
	app, database_name = _build_client(mongo_url, tmp_path)
	with TestClient(app) as test_client:
		yield test_client
	MongoClient(mongo_url).drop_database(database_name)


def _upload(client, content=INVOICE, name="Invoice", schema: dict | None = SCHEMA, **kwargs):
	form = {"name": name, **({"json_schema": json.dumps(schema)} if schema is not None else {})}
	return client.post("/templates", files={"file": ("t.html", content.encode(), "text/html")}, data=form, **kwargs)


def _render(
	client,
	template_id,
	data=DATA,
	version: int | None = None,
	correlation_id: str | None = None,
	output_format: str | None = None,
):
	body = {"template_data": data} | ({"correlation_id": correlation_id} if correlation_id is not None else {})
	if output_format is not None:
		body["output_format"] = output_format
	params = {"version": version} if version is not None else None
	return client.post(f"/templates/{template_id}/documents", json=body, params=params)


def _docx_text(content: bytes) -> str:
	with zipfile.ZipFile(io.BytesIO(content)) as docx:
		xml = docx.read("word/document.xml").decode()
	return html.unescape(" ".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", xml)))


def _mcp(client, tool: str, **arguments) -> dict:
	body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
	return client.post("/mcp", headers=MCP_HEADERS, json=body).json()["result"]


def test_upload_and_version_a_template(client):
	created = _upload(client)
	assert created.status_code == 201
	template = created.json()
	assert re.fullmatch(r"tpl_[0-9a-f]{32}", template["template_id"])
	assert template["latest_version"] == 1
	assert template["json_schema"] == SCHEMA

	template_id = template["template_id"]
	updated = client.post(
		f"/templates/{template_id}/versions", files={"file": ("t.html", b"<h1>{{ title }}</h1>", "text/html")}
	).json()
	assert updated["latest_version"] == 2
	assert [v["version"] for v in updated["versions"]] == [1, 2]
	assert updated["json_schema"] is None

	v1 = client.get(f"/templates/{template_id}/versions/1").json()
	assert v1["content"] == INVOICE
	assert v1["has_schema"]


def test_list_templates(client):
	assert client.get("/templates").json() == {"templates": [], "total": 0}
	ids = [_upload(client, name=f"Template {i}").json()["template_id"] for i in range(3)]

	listed = client.get("/templates").json()
	assert listed["total"] == 3
	templates = listed["templates"]
	assert {t["template_id"] for t in templates} == set(ids)
	# Newest first. Uploads in the same millisecond tie, so check the order rather than exact positions.
	created = [datetime.fromisoformat(t["created_at"]) for t in templates]
	assert created == sorted(created, reverse=True)
	# Summaries only: content, schemas and versions come from GET /templates/{id}.
	assert set(templates[0]) == {"template_id", "name", "description", "latest_version", "created_at", "updated_at"}

	pages = [client.get("/templates", params={"limit": 2, "offset": offset}).json() for offset in (0, 2)]
	assert [t["template_id"] for page in pages for t in page["templates"]] == [t["template_id"] for t in templates]
	assert [page["total"] for page in pages] == [3, 3]
	assert client.get("/templates", params={"limit": 0}).status_code == 422
	assert client.get("/templates", params={"limit": 201}).status_code == 422


def test_invalid_uploads_are_rejected_without_using_a_version(client):
	template_id = _upload(client).json()["template_id"]

	def add(content: bytes, **form):
		files = {"file": ("t.html", content, "text/html")}
		return client.post(f"/templates/{template_id}/versions", files=files, data=form)

	syntax = add(b"<p>{% for x in %}</p>")
	assert syntax.status_code == 422
	assert "line 1" in syntax.json()["detail"]
	assert add(b"\xff\xfe").status_code == 422
	assert add(b"<p>ok</p>", json_schema="{not json").status_code == 422
	assert add(b"<p>ok</p>", json_schema='{"type": "no-such-type"}').status_code == 422
	assert client.get(f"/templates/{template_id}").json()["latest_version"] == 1


def test_oversized_template_is_rejected(client):
	limit = make_settings().templates.max_template_bytes
	assert _upload(client, content="x" * (limit + 1), schema=None).status_code == 413


def test_render_and_download_pdf(client):
	template_id = _upload(client).json()["template_id"]
	rendered = _render(client, template_id)
	assert rendered.status_code == 201
	document = rendered.json()
	assert re.fullmatch(r"doc_[0-9a-f]{32}", document["document_id"])
	assert document["template_version"] == 1
	assert document["format"] == "pdf"
	assert document["page_count"] == 1
	assert document["filename"] == "Invoice-v1.pdf"

	pdf = client.get(f"/documents/{document['document_id']}/file")
	assert pdf.status_code == 200
	assert pdf.headers["content-type"] == "application/pdf"
	assert pdf.content.startswith(b"%PDF-")
	# The path from before other formats existed still works.
	assert client.get(f"/documents/{document['document_id']}/pdf").content == pdf.content

	signed = client.get(document["download_url"])
	assert signed.status_code == 200
	assert signed.content == pdf.content

	fresh = client.get(f"/documents/{document['document_id']}").json()
	assert fresh["download_url"].startswith("http://testserver/files/")


def test_render_and_download_docx(client):
	template_id = _upload(client).json()["template_id"]
	rendered = _render(client, template_id, output_format="docx")
	assert rendered.status_code == 201
	document = rendered.json()
	assert document["format"] == "docx"
	assert document["page_count"] is None
	assert document["filename"] == "Invoice-v1.docx"

	docx = client.get(f"/documents/{document['document_id']}/file")
	assert docx.status_code == 200
	assert docx.headers["content-type"] == DOCX_TYPE
	assert _docx_text(docx.content) == "Invoice INV-1 Acme Widget: 9.5"
	assert client.get(document["download_url"]).content == docx.content


def test_unknown_output_format_is_rejected(client):
	template_id = _upload(client).json()["template_id"]
	response = _render(client, template_id, output_format="xlsx")
	assert response.status_code == 422
	assert response.json()["detail"][0]["loc"] == ["body", "output_format"]


def test_render_a_specific_version(client):
	template_id = _upload(client).json()["template_id"]
	client.post(f"/templates/{template_id}/versions", files={"file": ("t.html", b"<p>{{ title }}</p>", "text/html")})
	assert _render(client, template_id, version=1).json()["template_version"] == 1
	assert _render(client, template_id, data={"title": "x"}).json()["template_version"] == 2
	assert _render(client, template_id, version=9).status_code == 404


def test_data_is_validated_against_schema(client):
	template_id = _upload(client).json()["template_id"]
	bad = _render(
		client, template_id, data={"number": 1, "customer": {}, "items": [{"description": "x", "price": "free"}]}
	)
	assert bad.status_code == 422
	assert bad.json()["detail"]["errors"] == [
		"customer: 'name' is a required property",
		"items[0].price: 'free' is not of type 'number'",
		"number: 1 is not of type 'string'",
	]


def test_missing_variables_are_reported_without_a_schema(client):
	template_id = _upload(client, schema=None).json()["template_id"]
	response = _render(client, template_id, data={"number": "INV-1"})
	assert response.status_code == 422
	assert "'customer' is undefined" in response.json()["detail"]["message"]


def test_templates_are_sandboxed(client):
	escape = _upload(client, content="{{ ''.__class__.__mro__[1].__subclasses__() }}", schema=None).json()
	response = _render(client, escape["template_id"], data={})
	assert response.status_code == 422
	assert "unsafe" in response.json()["detail"]

	include = _upload(client, content="{% include '/etc/passwd' %}", schema=None).json()
	assert _render(client, include["template_id"], data={}).status_code == 422


def test_rendered_values_are_html_escaped(client):
	template_id = _upload(client, content="<p>{{ name }}</p>", schema=None).json()["template_id"]
	data = {"name": "<img src=x>"}
	document_id = _render(client, template_id, data=data).json()["document_id"]
	assert client.get(f"/documents/{document_id}/file").status_code == 200

	# Shown as text, not parsed as an element.
	document_id = _render(client, template_id, data=data, output_format="docx").json()["document_id"]
	assert _docx_text(client.get(f"/documents/{document_id}/file").content) == "<img src=x>"


def test_templates_cannot_fetch_urls_or_files(client):
	hits: list[str] = []

	class Recorder(BaseHTTPRequestHandler):
		def do_GET(self) -> None:
			hits.append(self.path)
			self.send_response(200)
			self.end_headers()

		def log_message(self, format: str, *args: object) -> None:
			pass

	server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	url = f"http://127.0.0.1:{server.server_address[1]}"
	content = f"""<link rel="stylesheet" href="{url}/style.css"><img src="{url}/pixel.png">
	<img src="file:///etc/hosts"><style>@import url("{url}/import.css");</style>
	<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">"""
	try:
		template_id = _upload(client, content=content, schema=None).json()["template_id"]
		assert _render(client, template_id, data={}).status_code == 201
		docx = _render(client, template_id, data={}, output_format="docx").json()
	finally:
		server.shutdown()
	assert hits == []

	# Only the data: image made it into the Word document.
	with zipfile.ZipFile(io.BytesIO(client.get(f"/documents/{docx['document_id']}/file").content)) as archive:
		media = [name for name in archive.namelist() if name.startswith("word/media/")]
	assert len(media) == 1
	assert media[0].endswith(".gif")


def test_unknown_ids_are_not_found(client):
	assert client.get("/templates/tpl_missing").status_code == 404
	assert _render(client, "tpl_missing").status_code == 404
	assert client.get("/documents/doc_missing").status_code == 404
	assert client.get("/documents/doc_missing/file").status_code == 404


def test_render_data_size_limit(client):
	template_id = _upload(client, content="<p>ok</p>", schema=None).json()["template_id"]
	response = _render(client, template_id, data={"blob": "x" * (1024 * 1024)})
	assert response.status_code == 422
	assert "exceeds" in response.json()["detail"]["message"]


def test_mcp_tools_end_to_end(client):
	created = _mcp(client, "create_template", name="Invoice", content=INVOICE, json_schema=SCHEMA)
	template_id = created["structuredContent"]["template_id"]

	assert _mcp(client, "get_template", template_id=template_id)["structuredContent"]["json_schema"] == SCHEMA
	source = _mcp(client, "get_template_version", template_id=template_id)["structuredContent"]
	assert source["content"] == INVOICE

	rendered = _mcp(client, "render_document", template_id=template_id, template_data=DATA, correlation_id="job-42")
	assert not rendered["isError"]
	assert [c["type"] for c in rendered["content"]] == ["text", "resource_link"]
	document = rendered["structuredContent"]
	assert document["correlation_id"] == "job-42"
	assert rendered["content"][1]["mimeType"] == "application/pdf"
	assert client.get(rendered["content"][1]["uri"]).content.startswith(b"%PDF-")

	again = _mcp(client, "get_document", document_id=document["document_id"])["structuredContent"]
	assert again["document_id"] == document["document_id"]

	docx = _mcp(client, "render_document", template_id=template_id, template_data=DATA, output_format="docx")
	assert docx["structuredContent"]["format"] == "docx"
	assert docx["content"][1]["mimeType"] == DOCX_TYPE
	assert docx["content"][1]["name"] == "Invoice-v1.docx"
	assert "Widget: 9.5" in _docx_text(client.get(docx["content"][1]["uri"]).content)

	invalid = _mcp(client, "render_document", template_id=template_id, template_data={"number": "x"})
	assert invalid["isError"]
	assert "(root): 'customer' is a required property" in invalid["content"][0]["text"]


def test_api_routes_use_the_mcp_auth(mongo_url, tmp_path):
	app, database_name = _build_client(mongo_url, tmp_path, auth=(ApiKeyVerifier([SecretStr("k1")]),))
	try:
		with TestClient(app) as client:
			assert _upload(client).status_code == 401
			assert client.get("/templates").status_code == 401
			assert client.get("/documents/doc_x").status_code == 401
			assert _upload(client, headers={"Authorization": "Bearer k1"}).status_code == 201
			assert client.get("/health/ready").json()["checks"] == {"mongo": "ok"}
	finally:
		MongoClient(mongo_url).drop_database(database_name)


def test_render_body_shape(client):
	template_id = _upload(client, content="<p>ok</p>", schema=None).json()["template_id"]
	url = f"/templates/{template_id}/documents"
	assert client.post(url, json={"template_data": [1, 2]}).status_code == 422
	assert client.post(url, json={"correlation_id": "x"}).status_code == 422
	assert client.post(url, content=b"not json").status_code == 422
	# Unknown fields are rejected, so the unwrapped or old {"data": ...} shape can't render by accident.
	legacy = client.post(url, json={"data": {}})
	assert legacy.status_code == 422
	assert {e["loc"][-1] for e in legacy.json()["detail"]} == {"template_data", "data"}
	assert _render(client, template_id, data={}).status_code == 201


def test_correlation_id_is_returned_and_stored(client):
	template_id = _upload(client).json()["template_id"]
	rendered = _render(client, template_id, correlation_id="order-8841").json()
	assert rendered["correlation_id"] == "order-8841"
	assert client.get(f"/documents/{rendered['document_id']}").json()["correlation_id"] == "order-8841"
	assert _render(client, template_id).json()["correlation_id"] is None


@pytest.mark.parametrize("correlation_id", ["", "x" * 129, "line\nbreak", "tab\there"])
def test_invalid_correlation_ids_are_rejected(client, correlation_id):
	template_id = _upload(client, content="<p>ok</p>", schema=None).json()["template_id"]
	assert _render(client, template_id, data={}, correlation_id=correlation_id).status_code == 422
