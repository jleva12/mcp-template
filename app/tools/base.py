"""Class-based MCP tools, resources and prompts.

Group related MCP components in a `Toolset` subclass and mark methods with
`@mcp_tool`, `@mcp_resource` or `@mcp_prompt` (re-exported from FastMCP's
MCPMixin). Set `namespace` to prefix every component name, e.g. namespace
"billing" turns tool `refund` into `billing_refund`.

	class BillingTools(Toolset):
		namespace = "billing"

		@mcp_tool(annotations=ToolAnnotations(destructive_hint=True))
		async def refund(self, invoice_id: str, ctx: Context) -> Refund:
			\"\"\"Refund an invoice.\"\"\"  # the docstring becomes the tool description
			...

Raise `fastmcp.exceptions.ToolError` for errors the client should see; any
other exception is logged with a traceback and masked when
`mcp.mask_error_details` is on. FastMCP logs ToolErrors at ERROR by default,
so pass `log_level=logging.WARNING` for expected failures (bad input, not found)
to keep them out of error alerting.

Like `Routes`, pass the class to `ServerBuilder.with_toolsets`, or an instance
when the toolset takes extra dependencies in `__init__`.

For a single tool, skip the class and pass a `Tool` wrapping a plain function. Its
docstring becomes the description and its type hints the input schema; FastMCP's
`Depends`, `CurrentContext()` and `CurrentAccessToken()` parameters are injected
and left out of the schema. FastMCP only sees them as defaults, not in `Annotated`:

	async def lookup_order(order_id: str, db: Db = Depends(get_db)) -> Order:
		\"\"\"Look up an order by ID.\"\"\"
		...

	ServerBuilder(settings).with_toolsets(
		Tool(lookup_order, annotations=ToolAnnotations(read_only_hint=True)),
		Tool(refund_order, name="refund", auth=require_scopes("billing:write")),
	)
"""

from collections.abc import Callable
from typing import Any, ClassVar

from fastmcp import FastMCP
from fastmcp.contrib.mcp_mixin import MCPMixin, mcp_prompt, mcp_resource, mcp_tool
from fastmcp.tools import Tool as FastMCPTool

from app.core.settings import Settings

__all__ = ["Tool", "Toolset", "ToolsetSource", "mcp_prompt", "mcp_resource", "mcp_tool"]

# What ServerBuilder.with_toolsets accepts.
type ToolsetSource = type[Toolset] | Toolset | Tool


class Toolset(MCPMixin):
	namespace: ClassVar[str | None] = None

	def __init__(self, settings: Settings) -> None:
		self.settings = settings

	def register(self, server: FastMCP) -> None:
		self.register_all(server, prefix=self.namespace)


class Tool:
	"""
	One MCP tool from a plain function, for when a :class:`Toolset` class is more than you need.

	Not to be confused with FastMCP's ``Tool``: this is a component for ``ServerBuilder.with_toolsets``
	that builds one when the server is created.

	:param fn: The tool function, sync or async. Its name is the tool's name unless ``name`` is given.
	:type fn: Callable[..., Any]
	:param options: Forwarded to FastMCP's ``Tool.from_function``: ``name``, ``description``,
	    ``annotations``, ``tags``, ``output_schema``, ``timeout``, ``auth`` (per-tool checks such as
	    ``require_scopes``), ...
	"""

	def __init__(self, fn: Callable[..., Any], **options: Any) -> None:
		self.fn = fn
		self.options = options

	@property
	def name(self) -> str:
		return self.options.get("name") or getattr(self.fn, "__name__", repr(self.fn))

	def register(self, server: FastMCP) -> None:
		server.add_tool(FastMCPTool.from_function(self.fn, **self.options))

	def __repr__(self) -> str:
		return f"Tool({self.name})"
