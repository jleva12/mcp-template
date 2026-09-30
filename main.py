from app.core.auth import auth_from_settings
from app.core.database import Database
from app.core.files import FileStore
from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.models import MODELS
from app.routes import DocumentRoutes, FileRoutes, HealthRoutes, HelloRoutes, TemplateRoutes
from app.server import ServerBuilder
from app.services.rendering import DocxRenderer, PdfRenderer, RendererRegistry
from app.services.templates import TemplateService
from app.tools import FileTools, GreetingTools, IdentityTools, TemplateTools

settings = get_settings()
configure_logging(settings)

database = Database(settings, MODELS)
files = FileStore.from_settings(settings)
# One renderer per output format. Register more here, or swap the engine behind a format.
renderers = RendererRegistry(PdfRenderer(settings), DocxRenderer(settings))
templates = TemplateService(settings, renderers, files)

server = (
	ServerBuilder(settings)
	# API keys or JWTs per APP_MCP__AUTH__MODE. Swap in any FastMCP provider (e.g. GitHubProvider,
	# OIDCProxy for OAuth), or pass several to accept any of them. Also protects API route
	# classes with requires_auth = True.
	.with_auth(auth_from_settings(settings))
	.with_lifespan(database.lifespan)
	.with_routes(
		HealthRoutes(settings, checks={"mongo": database.ping, **renderers.readiness_checks()}),
		HelloRoutes,
		FileRoutes(settings, files),
		TemplateRoutes(settings, templates),
		DocumentRoutes(settings, templates),
	)
	.with_toolsets(GreetingTools, IdentityTools, FileTools(settings, files), TemplateTools(settings, templates))
)  # fmt: skip
app = server.build()

if __name__ == "__main__":
	server.run()
