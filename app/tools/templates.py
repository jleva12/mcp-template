import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

from app.core.settings import Settings
from app.models import OutputFormat, RenderedDocument
from app.services.errors import InvalidDataError, ServiceError
from app.services.templates import (
	CorrelationId,
	DocumentInfo,
	TemplateInfo,
	TemplateService,
	TemplateVersionDetail,
)
from app.tools.base import Toolset, mcp_tool

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


class TemplateTools(Toolset):
	"""
	MCP tools for creating versioned Jinja templates and rendering them to PDF or DOCX.

	:param settings: Application settings.
	:type settings: Settings
	:param service: Shared with the REST routes.
	:type service: TemplateService
	"""

	def __init__(self, settings: Settings, service: TemplateService) -> None:
		super().__init__(settings)
		self.service = service

	@mcp_tool()
	async def create_template(
		self,
		name: str,
		content: str,
		json_schema: dict[str, Any] | None = None,
		description: str | None = None,
	) -> TemplateInfo:
		"""
		Create a Jinja HTML template for generating PDF or Word (DOCX) documents; returns its template_id.

		`content` is one self-contained HTML file: put CSS in a <style> element and images in
		data: URIs; external URLs are not loaded. Use CSS paged media for PDF layout, e.g.
		`@page { size: A4; margin: 2cm }`. DOCX output keeps the HTML's structure (headings,
		paragraphs, lists, tables, bold/italic, links, images) but not its CSS, so use semantic
		elements rather than styled <div>s. Variables use Jinja syntax ({{ customer.name }},
		{% for item in items %}). Referencing a value the data doesn't provide is an error, so
		guard optional ones with {% if x is defined %} or {{ x | default('') }}.
		`json_schema` optionally describes the data render_document must receive.
		"""
		with tool_errors():
			return await self.service.create_template(name, content, json_schema, description)

	@mcp_tool()
	async def add_template_version(
		self, template_id: str, content: str, json_schema: dict[str, Any] | None = None
	) -> TemplateInfo:
		"""
		Save changed content as a new version of a template; it becomes the default for rendering.

		Earlier versions stay available. Pass the full HTML (see create_template for the rules)
		and the JSON Schema again if the new version should have one.
		"""
		with tool_errors():
			return await self.service.add_version(template_id, content, json_schema)

	@mcp_tool(annotations=_READ_ONLY)
	async def get_template(self, template_id: str) -> TemplateInfo:
		"""Get a template's name, versions, and the JSON Schema render data must match."""
		with tool_errors():
			return await self.service.get_template(template_id)

	@mcp_tool(annotations=_READ_ONLY)
	async def get_template_version(self, template_id: str, version: int | None = None) -> TemplateVersionDetail:
		"""Get the HTML source and JSON Schema of a template version (the latest if omitted)."""
		with tool_errors():
			return await self.service.get_version(template_id, version)

	@mcp_tool(output_schema=DocumentInfo.model_json_schema())
	async def render_document(
		self,
		template_id: str,
		template_data: dict[str, Any],
		version: int | None = None,
		correlation_id: CorrelationId | None = None,
		output_format: OutputFormat = OutputFormat.PDF,
	) -> ToolResult:
		"""
		Render a document from a template and JSON data; returns the document_id and a download link.

		`template_data` must match the template's JSON Schema (see get_template). `output_format` is
		"pdf" (default) or "docx" for an editable Word document. Renders the latest version unless
		`version` is given. `correlation_id` is an optional ID of your own (e.g. an order number)
		that is returned with the document for tracking.
		"""
		with tool_errors():
			document = await self.service.render(template_id, template_data, version, correlation_id, output_format)
		return self._document_result(document)

	@mcp_tool(annotations=_READ_ONLY, output_schema=DocumentInfo.model_json_schema())
	async def get_document(self, document_id: str) -> ToolResult:
		"""Get a rendered document's details and a fresh download link."""
		with tool_errors():
			document = await self.service.get_document(document_id)
		return self._document_result(document)

	def _document_result(self, document: RenderedDocument) -> ToolResult:
		info = self.service.document_info(document)
		return ToolResult(
			content=[TextContent(type="text", text=info.model_dump_json()), self.service.resource_link(document)],
			structured_content=info.model_dump(mode="json"),
		)


@contextmanager
def tool_errors() -> Iterator[None]:
	try:
		yield
	except InvalidDataError as exc:
		raise ToolError("\n".join([str(exc), *exc.errors]), log_level=logging.WARNING) from None
	except ServiceError as exc:
		raise ToolError(str(exc), log_level=logging.WARNING) from None
