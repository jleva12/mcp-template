"""An MCP App: an interactive template studio the chat client shows when the model opens it.

MCP Apps (the ``io.modelcontextprotocol/ui`` extension) let a tool name a ``ui://`` resource in
its ``_meta.ui.resourceUri``. Clients that support the extension (Claude, ChatGPT, VS Code, ...)
render that resource's HTML in a sandboxed iframe next to the tool call and pass it the tool's
result; the page can then call this server's tools through the client. Clients without the
extension ignore the metadata and just show the tool result.

Here, the model calls ``open_template_studio``. The studio page (``ui/template_studio.html``)
shows a live HTML preview as the user edits the data (via the app-only ``preview_template``
tool) and renders PDF or DOCX files with ``render_document`` from :class:`TemplateTools`.
"""

from pathlib import Path
from typing import Any

from fastmcp.apps import AppConfig, app_config_to_meta_dict
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import BaseModel

from app.core.settings import Settings
from app.services.templates import TemplateInfo, TemplatePreview, TemplateService, TemplateVersionDetail
from app.tools.base import Toolset, mcp_resource, mcp_tool
from app.tools.templates import tool_errors

STUDIO_URI = "ui://templates/studio.html"
_STUDIO_HTML = Path(__file__).with_name("ui").joinpath("template_studio.html")

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


def _ui(**config: Any) -> dict[str, Any]:
	return {"ui": app_config_to_meta_dict(AppConfig(**config))}


class StudioSession(BaseModel):
	"""What the studio page starts from."""

	template: TemplateInfo
	source: TemplateVersionDetail
	# The data to fill the template with first; the page builds a skeleton from the schema when None.
	template_data: dict[str, Any] | None


class TemplateStudioTools(Toolset):
	"""
	The template studio MCP App: its UI resource, the tool that opens it, and its app-only tools.

	:param settings: Application settings.
	:type settings: Settings
	:param service: Shared with :class:`TemplateTools` and the REST routes.
	:type service: TemplateService
	"""

	def __init__(self, settings: Settings, service: TemplateService) -> None:
		super().__init__(settings)
		self.service = service

	@mcp_resource(STUDIO_URI, meta=_ui(prefers_border=True))
	def template_studio(self) -> str:
		"""Interactive template studio: live preview, data editor, and PDF/DOCX rendering."""
		# ui:// resources are served as text/html;profile=mcp-app, the MIME type MCP Apps require.
		return _STUDIO_HTML.read_text()

	@mcp_tool(annotations=_READ_ONLY, meta=_ui(resource_uri=STUDIO_URI))
	async def open_template_studio(
		self, template_id: str, version: int | None = None, template_data: dict[str, Any] | None = None
	) -> ToolResult:
		"""
		Show the user an interactive studio for a template: a live preview they can fill with data and
		render to PDF or DOCX themselves.

		Opens the latest version unless `version` is given. Pass `template_data` to prefill realistic
		sample data that matches the template's JSON Schema (see get_template).
		"""
		with tool_errors():
			template = await self.service.get_template(template_id)
			source = await self.service.get_version(template_id, version)
		session = StudioSession(template=template, source=source, template_data=template_data)
		# The page gets the structured content; the model only needs to know the studio is open.
		summary = f"Opened the template studio for {template.name!r} (version {source.version})."
		return ToolResult(
			content=[TextContent(type="text", text=summary)],
			structured_content=session.model_dump(mode="json"),
		)

	@mcp_tool(annotations=_READ_ONLY, meta=_ui(resource_uri=STUDIO_URI, visibility=["app"]))
	async def preview_template(
		self, template_id: str, template_data: dict[str, Any], version: int | None = None
	) -> TemplatePreview:
		"""Fill a template with data as HTML, for the studio's live preview."""
		with tool_errors():
			return await self.service.preview(template_id, template_data, version)
