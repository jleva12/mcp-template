import logging

from fastmcp.exceptions import ToolError
from mcp.types import ResourceLink

from app.core.files import FileStore
from app.core.settings import Settings
from app.tools.base import Toolset, mcp_tool


class FileTools(Toolset):
	"""
	Example of a toolset that produces files; replace with your own.

	Save the file with ``self.files.save(...)`` and return ``self.files.resource_link(file)``:
	the client gets a signed download link rather than the file's bytes.

	:param settings: Application settings.
	:type settings: Settings
	:param files: The same store ``FileRoutes`` serves from.
	:type files: FileStore
	"""

	def __init__(self, settings: Settings, files: FileStore) -> None:
		super().__init__(settings)
		self.files = files

	@mcp_tool()
	async def create_text_file(self, filename: str, content: str) -> ResourceLink:
		"""Save text as a downloadable file and return a link to it."""
		if not content:
			raise ToolError("content must not be empty", log_level=logging.WARNING)
		file = await self.files.save(content, filename=filename)
		return self.files.resource_link(file)
