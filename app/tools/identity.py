from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from app.tools.base import Toolset, mcp_tool


class Identity(BaseModel):
	client_id: str | None
	subject: str | None
	scopes: list[str]


class IdentityTools(Toolset):
	"""
	Example of reading the caller's identity in a tool.

	Use ``get_access_token()`` (or FastMCP's ``TokenClaim("...")`` dependency) for token claims.
	"""

	@mcp_tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
	async def whoami(self) -> Identity:
		"""Show the client and scopes this server sees for the current request."""
		token = get_access_token()
		return Identity(
			client_id=token.client_id if token else None,
			subject=token.subject if token else None,
			scopes=list(token.scopes) if token else [],
		)
