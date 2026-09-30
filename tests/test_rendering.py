"""Renderers on their own, without MongoDB. PDF rendering needs WeasyPrint's system libraries;
on macOS run pytest with DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib.
"""

import io
import re
import zipfile

import anyio
import pytest

from app.models import OutputFormat
from app.services.errors import InvalidTemplateError, RenderError, UnsupportedFormatError
from app.services.rendering import DocxRenderer, HtmlRenderer, PdfRenderer, RendererRegistry
from tests.conftest import make_settings


class EchoRenderer(HtmlRenderer):
	"""A stand-in engine: returns the filled-in HTML as the file."""

	format = OutputFormat.DOCX
	media_type = "text/html"

	async def ready(self) -> None:
		return None

	async def _convert(self, html: str) -> tuple[bytes, int | None]:
		return html.encode(), None


class SlowRenderer(EchoRenderer):
	async def _convert(self, html: str) -> tuple[bytes, int | None]:
		await anyio.sleep(10)
		return b"", None


def _docx_paragraphs(content: bytes) -> list[str]:
	with zipfile.ZipFile(io.BytesIO(content)) as docx:
		xml = docx.read("word/document.xml").decode()
	paragraphs = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
	return ["".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", paragraph)) for paragraph in paragraphs]


def test_registry_looks_up_renderers_by_format():
	settings = make_settings()
	pdf, echo = PdfRenderer(settings), EchoRenderer(settings)
	registry = RendererRegistry(pdf, echo)
	assert registry.get(OutputFormat.PDF) is pdf
	# Swapping the engine behind a format is registering a different renderer for it.
	assert registry.get(OutputFormat.DOCX) is echo
	assert set(registry.readiness_checks()) == {"pdf", "docx"}


def test_registry_rejects_missing_and_duplicate_formats():
	settings = make_settings()
	with pytest.raises(UnsupportedFormatError, match="available formats: pdf"):
		RendererRegistry(PdfRenderer(settings)).get(OutputFormat.DOCX)
	with pytest.raises(ValueError, match="More than one renderer for docx"):
		RendererRegistry(DocxRenderer(settings), EchoRenderer(settings))
	with pytest.raises(ValueError, match="At least one"):
		RendererRegistry()


def test_registry_validates_templates_with_every_renderer():
	registry = RendererRegistry(PdfRenderer(make_settings()), DocxRenderer(make_settings()))
	registry.validate("<p>{{ ok }}</p>")
	with pytest.raises(InvalidTemplateError, match="line 1"):
		registry.validate("<p>{% if %}</p>")


async def test_html_renderers_share_the_sandboxed_jinja_step():
	rendered = await EchoRenderer(make_settings()).render("<p>{{ name }}</p>", {"name": "<b>"})
	assert rendered.content == b"<p>&lt;b&gt;</p>"
	assert rendered.page_count is None
	assert rendered.render_ms >= 0


async def test_render_timeout_covers_the_conversion():
	renderer = SlowRenderer(make_settings(templates={"render_timeout_seconds": 0.1}))
	with pytest.raises(RenderError, match=r"longer than 0\.1 seconds"):
		await renderer.render("<p>ok</p>", {})


async def test_docx_keeps_structure_and_drops_the_html_title():
	content = """<html><head><title>{{ title }}</title><style>h1 { color: red }</style></head><body>
	<h1>{{ title }}</h1><p><strong>Bold</strong> text</p><ul><li>one</li><li>two</li></ul>
	<table><tr><th>Item</th></tr><tr><td>Widget</td></tr></table></body></html>"""
	renderer = DocxRenderer(make_settings())
	await renderer.ready()
	rendered = await renderer.render(content, {"title": "Report"})
	assert rendered.page_count is None
	# The title shows once, from the <h1>; pandoc would otherwise add the <title> above it.
	assert _docx_paragraphs(rendered.content) == ["Report", "Bold text", "one", "two", "Item", "Widget"]
