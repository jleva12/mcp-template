import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app.core.settings import Settings
from app.models import OutputFormat
from app.routes.base import Routes, get, post
from app.services.errors import InvalidDataError, NotFoundError, ServiceError
from app.services.templates import (
	CorrelationId,
	DocumentInfo,
	TemplateInfo,
	TemplateService,
	TemplateVersionDetail,
)


class RenderRequest(BaseModel):
	# Unknown fields are errors, so a typo like "data" can't silently render without variables.
	model_config = ConfigDict(extra="forbid")

	template_data: dict[str, Any] = Field(
		description="The template's variables as a JSON object; must match its JSON Schema if it has one"
	)
	correlation_id: CorrelationId | None = None
	output_format: OutputFormat = Field(default=OutputFormat.PDF, description="The file type to produce")


class TemplateRoutes(Routes):
	"""
	REST API for uploading versioned Jinja templates and rendering them to PDF or DOCX.

	Uploads are a single HTML file with CSS inlined in ``<style>`` and images as ``data:``
	URIs; external URLs are not loaded when rendering.

	:param settings: Application settings.
	:type settings: Settings
	:param service: Shared with the MCP tools.
	:type service: TemplateService
	"""

	prefix = "/templates"
	tags = ["templates"]
	requires_auth = True

	def __init__(self, settings: Settings, service: TemplateService) -> None:
		super().__init__(settings)
		self.service = service

	@post("", status_code=201, summary="Upload a template")
	async def create_template(
		self,
		file: Annotated[UploadFile, File(description="Jinja HTML template with CSS and images inlined")],
		name: Annotated[str, Form(description="Template name; also used for document filenames")],
		description: Annotated[str | None, Form()] = None,
		json_schema: Annotated[str | None, Form(description="JSON Schema for render data, as JSON text")] = None,
	) -> TemplateInfo:
		content = await self._read_template(file)
		with _http_errors():
			return await self.service.create_template(name, content, _parse_schema(json_schema), description)

	@post("/{template_id}/versions", status_code=201, summary="Upload a new template version")
	async def add_version(
		self,
		template_id: str,
		file: Annotated[UploadFile, File(description="Jinja HTML template with CSS and images inlined")],
		json_schema: Annotated[str | None, Form(description="JSON Schema for render data, as JSON text")] = None,
	) -> TemplateInfo:
		content = await self._read_template(file)
		with _http_errors():
			return await self.service.add_version(template_id, content, _parse_schema(json_schema))

	@get("/{template_id}", summary="Get a template and its versions")
	async def get_template(self, template_id: str) -> TemplateInfo:
		with _http_errors():
			return await self.service.get_template(template_id)

	@get("/{template_id}/versions/{version}", summary="Get a template version's source and schema")
	async def get_version(self, template_id: str, version: int) -> TemplateVersionDetail:
		with _http_errors():
			return await self.service.get_version(template_id, version)

	@post("/{template_id}/documents", status_code=201, summary="Render a document from a template")
	async def render(
		self,
		template_id: str,
		request: RenderRequest,
		version: Annotated[int | None, Query(ge=1, description="Template version; the latest when omitted")] = None,
	) -> DocumentInfo:
		with _http_errors():
			document = await self.service.render(
				template_id, request.template_data, version, request.correlation_id, request.output_format
			)
			return self.service.document_info(document)

	async def _read_template(self, file: UploadFile) -> str:
		limit = self.settings.templates.max_template_bytes
		raw = await file.read(limit + 1)
		if len(raw) > limit:
			raise HTTPException(status_code=413, detail=f"Template exceeds {limit} bytes")
		try:
			return raw.decode("utf-8")
		except UnicodeDecodeError:
			raise HTTPException(status_code=422, detail="Template must be UTF-8 text") from None


class DocumentRoutes(Routes):
	"""
	REST API for rendered documents.

	:param settings: Application settings.
	:type settings: Settings
	:param service: Shared with the MCP tools.
	:type service: TemplateService
	"""

	prefix = "/documents"
	tags = ["documents"]
	requires_auth = True

	def __init__(self, settings: Settings, service: TemplateService) -> None:
		super().__init__(settings)
		self.service = service

	@get("/{document_id}", summary="Get a document with a fresh download link")
	async def get_document(self, document_id: str) -> DocumentInfo:
		with _http_errors():
			return self.service.document_info(await self.service.get_document(document_id))

	@get("/{document_id}/file", summary="Download a document's file", response_class=Response)
	# The path from before documents could be other formats; it serves any format.
	@get("/{document_id}/pdf", summary="Download a document's file", response_class=Response, deprecated=True)
	async def download(self, document_id: str) -> Response:
		with _http_errors():
			return await self.service.file_response(await self.service.get_document(document_id))


def _parse_schema(raw: str | None) -> dict[str, Any] | None:
	if raw is None or not raw.strip():
		return None
	try:
		schema = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise HTTPException(status_code=422, detail=f"json_schema is not valid JSON: {exc}") from None
	if not isinstance(schema, dict):
		raise HTTPException(status_code=422, detail="json_schema must be a JSON object")
	return schema


@contextmanager
def _http_errors() -> Iterator[None]:
	try:
		yield
	except NotFoundError as exc:
		raise HTTPException(status_code=404, detail=str(exc)) from None
	except InvalidDataError as exc:
		raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.errors}) from None
	except ServiceError as exc:
		raise HTTPException(status_code=422, detail=str(exc)) from None
