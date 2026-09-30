from app.routes.base import Route, Routes, RouteSource, delete, get, patch, post, put, route
from app.routes.files import FileRoutes
from app.routes.health import HealthRoutes
from app.routes.hello import HelloRoutes
from app.routes.templates import DocumentRoutes, TemplateRoutes

__all__ = [
	"DocumentRoutes",
	"FileRoutes",
	"HealthRoutes",
	"HelloRoutes",
	"Route",
	"RouteSource",
	"Routes",
	"TemplateRoutes",
	"delete",
	"get",
	"patch",
	"post",
	"put",
	"route",
]
