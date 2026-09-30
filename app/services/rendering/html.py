"""Base for renderers that fill a Jinja HTML template, then convert the HTML to their format.

Uploaded templates are treated as untrusted code:

- Jinja runs in its immutable sandbox with HTML autoescaping: templates can't reach Python
  internals, the filesystem (no loader, so no ``include``/``extends``), or mutate data.
- Undefined variables raise instead of rendering blank, so missing data is reported by name.
  Templates use ``{% if x is defined %}`` or ``{{ x | default('') }}`` for optional fields.
- Converters load only ``data:`` URLs, so templates can't make the server fetch internal
  URLs or read local files. Everything a template needs must be inlined.
"""

import time
from abc import abstractmethod
from functools import lru_cache
from typing import Any

import anyio
import anyio.to_thread
from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
from jinja2.exceptions import SecurityError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.core.settings import Settings
from app.services.errors import InvalidDataError, InvalidTemplateError, RenderError
from app.services.rendering.base import RenderedFile, Renderer

_env = ImmutableSandboxedEnvironment(autoescape=True, undefined=StrictUndefined)
# Shared by all HTML renderers, so a template is compiled once whichever formats it's rendered to.
_compile = lru_cache(maxsize=128)(_env.from_string)


class HtmlRenderer(Renderer):
	"""
	Fills a Jinja HTML template with data, then converts the HTML with :meth:`_convert`.

	At most ``templates.render_concurrency`` documents render at once per renderer, and a render
	fails after ``templates.render_timeout_seconds``, including time spent waiting for a slot.

	:param settings: Application settings; ``settings.templates`` sets the limits.
	:type settings: Settings
	"""
	
	def __init__(self, settings: Settings) -> None:
		self.config = settings.templates
		self._limiter = anyio.CapacityLimiter(self.config.render_concurrency)
	
	def validate(self, content: str) -> None:
		"""
		Checks Jinja syntax without rendering.

		:param content: Jinja HTML source.
		:type content: str
		:raises InvalidTemplateError: With the line number of the first syntax error.
		"""
		try:
			_env.parse(content)
		except TemplateSyntaxError as exc:
			raise InvalidTemplateError(f"Template syntax error on line {exc.lineno}: {exc.message}") from None
	
	async def render(self, content: str, data: dict[str, Any]) -> RenderedFile:
		timeout = self.config.render_timeout_seconds
		try:
			with anyio.fail_after(timeout):
				async with self._limiter:
					start = time.perf_counter()
					html = await anyio.to_thread.run_sync(_fill, content, data, abandon_on_cancel=True)
					file, page_count = await self._convert(html)
		except TimeoutError:
			raise RenderError(f"Rendering took longer than {timeout:g} seconds") from None
		return RenderedFile(file, render_ms=round((time.perf_counter() - start) * 1000, 2), page_count=page_count)
	
	@abstractmethod
	async def _convert(self, html: str) -> tuple[bytes, int | None]:
		"""
		Converts filled-in HTML to this renderer's format.

		Runs under the render timeout, so it must return promptly when cancelled: run blocking code
		with ``anyio.to_thread.run_sync(..., abandon_on_cancel=True)``, and processes with
		``anyio.run_process``, which kills them.

		:param html: The template's output.
		:type html: str
		:return: The file, and its page count if the format has fixed pages.
		:rtype: tuple[bytes, int | None]
		:raises RenderError: If the conversion fails.
		"""


def _fill(content: str, data: dict[str, Any]) -> str:
	template = _compile(content)
	try:
		return template.render(data)
	except UndefinedError as exc:
		raise InvalidDataError(f"Template data is missing a value: {exc.message}") from None
	except SecurityError as exc:
		raise RenderError(f"Template attempted an unsafe operation: {exc}") from None
	except Exception as exc:
		# Runtime errors inside the template, e.g. arithmetic on a string from the data.
		raise RenderError(f"Template failed to render: {type(exc).__name__}: {exc}") from None
