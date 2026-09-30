"""The renderer interface: one implementation per output format, looked up by format at render time.

To add a format, add it to :class:`~app.models.OutputFormat`, subclass :class:`Renderer` (or
:class:`~app.services.rendering.html.HtmlRenderer` when the engine converts HTML), and register
an instance in ``main.py``. To swap the engine behind a format, register a different renderer for it.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from app.models import OutputFormat
from app.services.errors import UnsupportedFormatError


@dataclass(frozen=True, slots=True)
class RenderedFile:
	content: bytes
	render_ms: float
	# None for formats without fixed pages: Word lays out a DOCX when it opens it.
	page_count: int | None = None


class Renderer(ABC):
	"""
	Renders a template with data into a file of one format.

	Templates are untrusted, so implementations must not let one read local files or make network
	requests, and must give up after ``templates.render_timeout_seconds``.

	:cvar format: The output format this renderer produces.
	:cvar media_type: MIME type of the files it produces.
	"""

	format: ClassVar[OutputFormat]
	media_type: ClassVar[str]

	@abstractmethod
	def validate(self, content: str) -> None:
		"""
		Checks that ``content`` is a template this renderer can render, without rendering it.

		:param content: Template source.
		:type content: str
		:raises InvalidTemplateError: Describing the first problem found.
		"""

	@abstractmethod
	async def render(self, content: str, data: dict[str, Any]) -> RenderedFile:
		"""
		Fills the template with ``data`` and produces the file.

		:param content: Template source.
		:type content: str
		:param data: Template variables.
		:type data: dict[str, Any]
		:rtype: RenderedFile
		:raises InvalidDataError: If the template uses a variable ``data`` doesn't provide.
		:raises RenderError: If the template fails at runtime or rendering times out.
		"""

	@abstractmethod
	async def ready(self) -> None:
		"""Readiness check: raises when the engine can't render, e.g. a system library is missing."""


class RendererRegistry:
	"""
	The renderer for each output format.

	A template must render in every registered format, so templates are validated against all of them.

	:param renderers: One per output format.
	:type renderers: Renderer
	:raises ValueError: If there are none, or two produce the same format.
	"""

	def __init__(self, *renderers: Renderer) -> None:
		if not renderers:
			raise ValueError("At least one renderer is required")
		self._renderers: dict[OutputFormat, Renderer] = {}
		for renderer in renderers:
			if renderer.format in self._renderers:
				raise ValueError(f"More than one renderer for {renderer.format}")
			self._renderers[renderer.format] = renderer

	def get(self, output_format: OutputFormat) -> Renderer:
		"""
		:raises UnsupportedFormatError: If no renderer produces ``output_format``.
		"""
		renderer = self._renderers.get(output_format)
		if renderer is None:
			available = ", ".join(self._renderers)
			raise UnsupportedFormatError(f"Can't render {output_format}; available formats: {available}")
		return renderer

	def validate(self, content: str) -> None:
		"""
		Checks the template against every renderer.

		:raises InvalidTemplateError: From the first renderer that rejects it.
		"""
		for renderer in self._renderers.values():
			renderer.validate(content)

	def readiness_checks(self) -> dict[str, Callable[[], Awaitable[None]]]:
		"""Each renderer's :meth:`Renderer.ready`, keyed by format, for ``HealthRoutes``."""
		return {str(output_format): renderer.ready for output_format, renderer in self._renderers.items()}
