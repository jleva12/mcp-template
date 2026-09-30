import logging

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from app.core.logging import get_logger
from app.tools.base import Toolset, mcp_prompt, mcp_resource, mcp_tool

log = get_logger(__name__)


class Greeting(BaseModel):
	message: str


class GreetingTools(Toolset):
	"""Example toolset; replace with your own."""

	@mcp_tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
	async def greet(self, name: str) -> Greeting:
		"""Greet someone by name."""
		if not name.strip():
			raise ToolError("name must not be empty", log_level=logging.WARNING)
		log.info("greeting.sent", name=name)
		return Greeting(message=f"Hello {name}")

	@mcp_resource("server://info", mime_type="application/json")
	def server_info(self) -> dict[str, str]:
		"""Name, version and environment of this server."""
		return {
			"name": self.settings.name,
			"version": self.settings.version,
			"environment": self.settings.environment,
		}

	@mcp_prompt()
	def welcome(self, name: str) -> str:
		"""Prompt that asks the model to write a welcome note."""
		return f"Write a short, friendly welcome message for {name}."
