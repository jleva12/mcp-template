"""Templates, their versions, and the documents (PDF, DOCX, ...) rendered from them.

Anyone holding a template or document ID can use it; IDs are unguessable (see app.models).
Templates are versioned: uploading changed content adds a new version, and rendering uses
the latest one unless a version is given. Each rendered document records the version used.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from beanie import UpdateResponse
from beanie.operators import Inc, Set
from jsonschema import Draft202012Validator, SchemaError
from jsonschema.validators import validator_for
from mcp.types import ResourceLink
from pydantic import BaseModel, Field
from referencing.exceptions import Unresolvable
from starlette.responses import Response

from app.core.files import FileStore, StoredFile
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models import DocumentFile, OutputFormat, RenderedDocument, Template, TemplateVersion
from app.services.errors import InvalidDataError, InvalidTemplateError, NotFoundError, RenderError
from app.services.rendering import RendererRegistry

log = get_logger("app.templates")

_MAX_REPORTED_ERRORS = 20

# Caller-supplied id echoed back on the document and attached to every log line of the render.
# Printable text only, so it can't break log lines.
type CorrelationId = Annotated[
	str,
	Field(
		min_length=1,
		max_length=128,
		pattern=r"^[^\x00-\x1f\x7f]+$",
		description="Your own ID for this generation (e.g. an order or job ID); returned on the document and logged",
	),
]


class TemplateVersionInfo(BaseModel):
	version: int
	sha256: str
	size_bytes: int
	has_schema: bool
	created_at: datetime


class TemplateInfo(BaseModel):
	template_id: str
	name: str
	description: str | None
	latest_version: int
	# JSON Schema of the latest version: the shape `data` must have when rendering.
	json_schema: dict[str, Any] | None
	versions: list[TemplateVersionInfo]
	created_at: datetime
	updated_at: datetime


class TemplateVersionDetail(TemplateVersionInfo):
	template_id: str
	content: str
	json_schema: dict[str, Any] | None


class DocumentInfo(BaseModel):
	document_id: str
	correlation_id: str | None
	template_id: str
	template_version: int
	format: OutputFormat
	filename: str
	size_bytes: int
	# None for formats without fixed pages (DOCX).
	page_count: int | None
	created_at: datetime
	download_url: str
	download_url_expires_at: datetime


class TemplateService:
	"""
	Creates and versions templates and renders them to stored files.

	:param settings: Application settings.
	:type settings: Settings
	:param renderers: The renderer for each output format.
	:type renderers: RendererRegistry
	:param files: Stores the rendered files and signs their download links.
	:type files: FileStore
	"""

	def __init__(self, settings: Settings, renderers: RendererRegistry, files: FileStore) -> None:
		self.config = settings.templates
		self.renderers = renderers
		self.files = files

	async def create_template(
		self,
		name: str,
		content: str,
		json_schema: dict[str, Any] | None = None,
		description: str | None = None,
	) -> TemplateInfo:
		"""
		Stores a new template as version 1.

		:param name: Human-readable name; also used for document filenames.
		:type name: str
		:param content: Jinja HTML with CSS and images inlined.
		:type content: str
		:param json_schema: Optional JSON Schema that render data must satisfy.
		:type json_schema: dict[str, Any] | None
		:param description: Optional description.
		:type description: str | None
		:rtype: TemplateInfo
		:raises InvalidTemplateError: If the content or schema is invalid or too large.
		"""
		name = name.strip()
		if not name:
			raise InvalidTemplateError("Template name must not be empty")
		template = Template(name=name, description=description, latest_version=1)
		version = self._new_version(template.id, 1, content, json_schema)
		# Version first: if the template insert fails, the orphaned version is unreachable.
		await version.insert()
		await template.insert()
		log.info("template.created", template_id=template.id, size_bytes=version.size_bytes)
		return await self.get_template(template.id)

	async def add_version(
		self, template_id: str, content: str, json_schema: dict[str, Any] | None = None
	) -> TemplateInfo:
		"""
		Adds a new version, which becomes the default for rendering. Earlier versions stay usable.

		:param template_id: The template to update.
		:type template_id: str
		:param content: Jinja HTML with CSS and images inlined.
		:type content: str
		:param json_schema: Optional JSON Schema for this version's render data.
		:type json_schema: dict[str, Any] | None
		:rtype: TemplateInfo
		:raises NotFoundError: If the template doesn't exist.
		:raises InvalidTemplateError: If the content or schema is invalid or too large.
		"""
		# Validate before claiming a version number, so bad uploads don't leave gaps.
		draft = self._new_version(template_id, 0, content, json_schema)
		template = await Template.find_one(Template.id == template_id).update(
			Inc({Template.latest_version: 1}),
			Set({Template.updated_at: datetime.now(UTC)}),
			response_type=UpdateResponse.NEW_DOCUMENT,
		)
		if not isinstance(template, Template):
			raise NotFoundError(f"Template {template_id!r} not found")
		draft.version = template.latest_version
		await draft.insert()
		log.info("template.version_added", template_id=template_id, version=draft.version)
		return await self.get_template(template_id)

	async def get_template(self, template_id: str) -> TemplateInfo:
		"""
		:raises NotFoundError: If the template doesn't exist.
		"""
		template = await self._template(template_id)
		versions = await TemplateVersion.find(TemplateVersion.template_id == template_id).sort("version").to_list()
		latest = next((v for v in versions if v.version == template.latest_version), None)
		return TemplateInfo(
			template_id=template.id,
			name=template.name,
			description=template.description,
			latest_version=template.latest_version,
			json_schema=_load_schema(latest),
			versions=[_version_info(v) for v in versions],
			created_at=template.created_at,
			updated_at=template.updated_at,
		)

	async def get_version(self, template_id: str, version: int | None = None) -> TemplateVersionDetail:
		"""
		Returns a version's source and schema; the latest when ``version`` is ``None``.

		:raises NotFoundError: If the template or version doesn't exist.
		"""
		record = await self._version(template_id, version)
		return TemplateVersionDetail(
			**_version_info(record).model_dump(),
			template_id=template_id,
			content=record.content,
			json_schema=_load_schema(record),
		)

	async def render(
		self,
		template_id: str,
		data: dict[str, Any],
		version: int | None = None,
		correlation_id: str | None = None,
		output_format: OutputFormat = OutputFormat.PDF,
	) -> RenderedDocument:
		"""
		Validates ``data`` against the version's schema, renders the file, and stores it.

		:param template_id: The template to render.
		:type template_id: str
		:param data: Template variables (a JSON object).
		:type data: dict[str, Any]
		:param version: Template version; the latest when ``None``.
		:type version: int | None
		:param correlation_id: Caller's ID for this generation; stored on the document and bound to
		    every log line of the render, including failures.
		:type correlation_id: str | None
		:param output_format: The file type to produce.
		:type output_format: OutputFormat
		:return: The stored document.
		:rtype: RenderedDocument
		:raises NotFoundError: If the template or version doesn't exist.
		:raises UnsupportedFormatError: If no renderer produces ``output_format``.
		:raises InvalidDataError: If ``data`` fails the schema or lacks a value the template uses.
		:raises RenderError: If the template fails while rendering or times out.
		"""
		if correlation_id is not None:
			# Bound for the rest of the request (not just this call), so the access log and MCP request
			# log lines carry it too, including when the render fails.
			structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
		return await self._render(template_id, data, version, correlation_id, output_format)

	async def _render(
		self,
		template_id: str,
		data: dict[str, Any],
		version: int | None,
		correlation_id: str | None,
		output_format: OutputFormat,
	) -> RenderedDocument:
		renderer = self.renderers.get(output_format)
		if len(json.dumps(data, default=str).encode()) > self.config.max_data_bytes:
			raise InvalidDataError(f"Render data exceeds {self.config.max_data_bytes} bytes")
		template = await self._template(template_id)
		record = await self._version(template_id, version, template)
		self._validate_data(record, data)

		rendered = await renderer.render(record.content, data)
		stored = await self.files.save(
			rendered.content,
			filename=f"{template.name}-v{record.version}.{renderer.format}",
			content_type=renderer.media_type,
		)
		document = RenderedDocument(
			template_id=template_id,
			template_version=record.version,
			format=renderer.format,
			file=DocumentFile(
				file_id=stored.id,
				filename=stored.filename,
				content_type=stored.content_type,
				size_bytes=stored.size,
			),
			page_count=rendered.page_count,
			render_ms=rendered.render_ms,
			correlation_id=correlation_id,
		)
		await document.insert()
		log.info(
			"document.rendered",
			document_id=document.id,
			template_id=template_id,
			template_version=record.version,
			format=renderer.format,
			page_count=rendered.page_count,
			size_bytes=stored.size,
			render_ms=rendered.render_ms,
		)
		return document

	async def get_document(self, document_id: str) -> RenderedDocument:
		"""
		:raises NotFoundError: If the document doesn't exist.
		"""
		document = await RenderedDocument.get(document_id)
		if document is None:
			raise NotFoundError(f"Document {document_id!r} not found")
		return document

	def document_info(self, document: RenderedDocument) -> DocumentInfo:
		"""Describes a document with a freshly signed download link."""
		expires_at = datetime.now(UTC) + timedelta(seconds=self.files.config.link_ttl_seconds)
		return DocumentInfo(
			document_id=document.id,
			correlation_id=document.correlation_id,
			template_id=document.template_id,
			template_version=document.template_version,
			format=document.format,
			filename=document.file.filename,
			size_bytes=document.file.size_bytes,
			page_count=document.page_count,
			created_at=document.created_at,
			download_url=self.files.download_url(_stored_file(document)),
			download_url_expires_at=expires_at,
		)

	def resource_link(self, document: RenderedDocument) -> ResourceLink:
		"""The document's download link as MCP ``resource_link`` content."""
		return self.files.resource_link(_stored_file(document))

	async def file_response(self, document: RenderedDocument) -> Response:
		"""
		Streams the document's file (local storage) or redirects to it (S3).

		:raises NotFoundError: If the stored file is gone.
		"""
		response = await self.files.download_response(document.file.file_id)
		if response is None:
			raise NotFoundError(f"The file for document {document.id!r} is no longer available")
		return response

	async def _template(self, template_id: str) -> Template:
		template = await Template.get(template_id)
		if template is None:
			raise NotFoundError(f"Template {template_id!r} not found")
		return template

	async def _version(
		self, template_id: str, version: int | None, template: Template | None = None
	) -> TemplateVersion:
		if version is None:
			version = (template or await self._template(template_id)).latest_version
		record = await TemplateVersion.find_one(
			TemplateVersion.template_id == template_id, TemplateVersion.version == version
		)
		if record is None:
			raise NotFoundError(f"Template {template_id!r} has no version {version}")
		return record

	def _new_version(
		self, template_id: str, version: int, content: str, json_schema: dict[str, Any] | None
	) -> TemplateVersion:
		encoded = content.encode()
		if not content.strip():
			raise InvalidTemplateError("Template content must not be empty")
		if len(encoded) > self.config.max_template_bytes:
			raise InvalidTemplateError(f"Template exceeds {self.config.max_template_bytes} bytes")
		self.renderers.validate(content)
		return TemplateVersion(
			template_id=template_id,
			version=version,
			content=content,
			json_schema=self._check_schema(json_schema),
			sha256=hashlib.sha256(encoded).hexdigest(),
			size_bytes=len(encoded),
		)

	def _check_schema(self, json_schema: dict[str, Any] | None) -> str | None:
		if json_schema is None:
			return None
		if not isinstance(json_schema, dict):
			raise InvalidTemplateError("JSON Schema must be a JSON object")
		serialized = json.dumps(json_schema)
		if len(serialized.encode()) > self.config.max_schema_bytes:
			raise InvalidTemplateError(f"JSON Schema exceeds {self.config.max_schema_bytes} bytes")
		try:
			validator_for(json_schema, default=Draft202012Validator).check_schema(json_schema)
		except SchemaError as exc:
			raise InvalidTemplateError(f"Invalid JSON Schema: {exc.message}") from None
		return serialized

	@staticmethod
	def _validate_data(record: TemplateVersion, data: dict[str, Any]) -> None:
		schema = _load_schema(record)
		if schema is None:
			return
		validator = validator_for(schema, default=Draft202012Validator)(schema)
		try:
			errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
		except Unresolvable as exc:
			# Remote $refs are never fetched.
			raise RenderError(f"The template's JSON Schema has an unresolvable $ref: {exc}") from None
		if errors:
			details = [
				f"{e.json_path.removeprefix('$.') if e.absolute_path else '(root)'}: {e.message}" for e in errors
			]
			raise InvalidDataError(
				"Render data doesn't match the template's JSON Schema", details[:_MAX_REPORTED_ERRORS]
			)


def _load_schema(record: TemplateVersion | None) -> dict[str, Any] | None:
	return json.loads(record.json_schema) if record is not None and record.json_schema else None


def _version_info(record: TemplateVersion) -> TemplateVersionInfo:
	return TemplateVersionInfo(
		version=record.version,
		sha256=record.sha256,
		size_bytes=record.size_bytes,
		has_schema=record.json_schema is not None,
		created_at=record.created_at,
	)


def _stored_file(document: RenderedDocument) -> StoredFile:
	file = document.file
	return StoredFile(id=file.file_id, filename=file.filename, content_type=file.content_type, size=file.size_bytes)
