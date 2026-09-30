"""HTML -> DOCX (Word) with pandoc.

Pandoc keeps the HTML's structure (headings, paragraphs, lists, tables, emphasis, links and
images) and maps it to Word's built-in styles, but ignores CSS: layout, colors, fonts and
``@page`` rules don't carry over.

Pandoc comes from the pypandoc_binary package, which bundles it; set PYPANDOC_PANDOC to use
another install. It runs with ``--sandbox``, so it loads only ``data:`` URLs, never files or
network resources, under a heap limit, and the render timeout kills it.
"""

from functools import lru_cache
from pathlib import Path

import anyio
import anyio.to_thread

from app.core.logging import get_logger
from app.models import OutputFormat
from app.services.errors import RenderError
from app.services.rendering.html import HtmlRenderer

log = get_logger("app.rendering.docx")

_MAX_HEAP_MB = 512
# The GHC runtime's exit status when pandoc hits the heap limit.
_HEAP_EXHAUSTED = 251
_DROP_TITLE = Path(__file__).with_name("drop_title.lua")
# Enough of pandoc's stderr for the logs, not a flood from a huge document.
_MAX_LOGGED_STDERR = 2000


class DocxRenderer(HtmlRenderer):
	"""Renders Jinja HTML templates to Word documents with pandoc."""

	format = OutputFormat.DOCX
	media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

	async def ready(self) -> None:
		"""Readiness check: finds the pandoc executable."""
		await anyio.to_thread.run_sync(_pandoc)

	async def _convert(self, html: str) -> tuple[bytes, int | None]:
		pandoc = await anyio.to_thread.run_sync(_pandoc)
		command = [
			pandoc,
			*("+RTS", f"-M{_MAX_HEAP_MB}m", "-RTS"),
			"--sandbox",
			"--from=html",
			"--to=docx",
			f"--lua-filter={_DROP_TITLE}",
			"--output=-",
		]
		result = await anyio.run_process(command, input=html.encode(), check=False)
		stderr = result.stderr.decode(errors="replace").strip()[-_MAX_LOGGED_STDERR:]
		if result.returncode == _HEAP_EXHAUSTED:
			raise RenderError(f"Rendering used more than {_MAX_HEAP_MB} MB of memory")
		if result.returncode != 0:
			log.warning("docx.pandoc_failed", returncode=result.returncode, stderr=stderr)
			raise RenderError("Converting the HTML to DOCX failed")
		if stderr:
			# e.g. images left out because they aren't data: URLs.
			log.warning("docx.pandoc_warnings", stderr=stderr)
		return result.stdout, None


@lru_cache(maxsize=1)
def _pandoc() -> str:
	import pypandoc

	try:
		return pypandoc.get_pandoc_path()
	except OSError as exc:
		raise RuntimeError("pandoc not found: run `uv sync` to install pypandoc_binary, which bundles it") from exc
