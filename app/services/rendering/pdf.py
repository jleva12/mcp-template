"""HTML -> PDF with WeasyPrint.

WeasyPrint only loads ``data:`` URLs, so templates can't make the server fetch internal URLs
or read local files.
"""

import sys
from functools import lru_cache
from types import ModuleType

import anyio
import anyio.to_thread

from app.models import OutputFormat
from app.services.rendering.html import HtmlRenderer


class PdfRenderer(HtmlRenderer):
	"""
	Handles the rendering of HTML into PDF format.

	This class extends `HtmlRenderer` to process HTML content into PDF files
	using WeasyPrint. It defines the PDF-specific output format and media type,
	and provides methods to perform readiness checks and conversions of HTML
	into PDF.

	:ivar format: Specifies the output format for the renderer.
	:type format: OutputFormat
	:ivar media_type: MIME type associated with the rendered PDF output.
	:type media_type: str
	"""

	format = OutputFormat.PDF
	media_type = "application/pdf"

	async def ready(self) -> None:
		"""Readiness check: loads WeasyPrint's native libraries (and warms the import)."""
		await anyio.to_thread.run_sync(_weasyprint)

	async def _convert(self, html: str) -> tuple[bytes, int | None]:
		return await anyio.to_thread.run_sync(_write_pdf, html, abandon_on_cancel=True)


def _write_pdf(html: str) -> tuple[bytes, int]:
	"""
	Generates a PDF from the provided HTML string and returns the binary PDF data
	along with the number of pages in the document.

	This function utilizes `weasyprint` to render the HTML into a PDF document.
	It employs a secure URL fetcher that limits the protocols to "data" for
	enhanced security when processing the HTML content.

	:param html: The HTML string to be converted into a PDF document.
	:type html: str
	:return: A tuple containing the binary PDF data and the number of pages in
	    the resulting document.
	:rtype: tuple[bytes, int]
	"""
	weasyprint = _weasyprint()
	fetcher = weasyprint.URLFetcher(allowed_protocols={"data"})
	document = weasyprint.HTML(string=html, url_fetcher=fetcher).render()
	return document.write_pdf(), len(document.pages)


@lru_cache(maxsize=1)
def _weasyprint() -> ModuleType:
	"""
	Loads and returns the WeasyPrint module, ensuring its system dependencies are
	appropriately installed. The function caches its result for efficiency. If the
	module fails to load due to missing or improperly configured dependencies, it provides
	platform-specific installation hints.

	:raises RuntimeError: If WeasyPrint's system libraries cannot be loaded. Provides
	                      platform-specific guidance for resolving the issue.
	:return: The imported WeasyPrint module.
	:rtype: ModuleType
	"""
	try:
		import weasyprint
	except OSError as exc:
		if sys.platform == "darwin":
			hint = "brew install pango, and start the app with DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib"
		else:
			hint = "install Pango, e.g. apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0"
		raise RuntimeError(f"WeasyPrint can't load its system libraries: {hint}") from exc
	return weasyprint
