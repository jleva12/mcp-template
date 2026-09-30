"""Class-based HTTP routing.

Group related endpoints in a `Routes` subclass and mark methods with the verb
decorators below. Each class becomes one `APIRouter`; `self` gives endpoints
access to settings and anything else the class sets up in `__init__`.

	class UserRoutes(Routes):
		prefix = "/users"
		tags = ["users"]

		@get("/{user_id}", response_model=User)
		async def get_user(self, user_id: int) -> User:
			...

Decorator kwargs are forwarded to `APIRouter.add_api_route` (response_model,
status_code, dependencies, summary, ...). Routes register in definition order,
so declare static paths before parametrised ones.

Pass the class to `ServerBuilder.with_routes` and it is constructed with settings.
To inject dependencies shared with other routes or toolsets (a file store, a DB
pool, ...), take them in `__init__` and pass an instance instead:

	class FileRoutes(Routes):
		def __init__(self, settings: Settings, files: FileStore) -> None:
			super().__init__(settings)
			self.files = files

	ServerBuilder(settings).with_routes(FileRoutes(settings, files))

For a single endpoint, skip the class and pass a `Route` with a path, a handler
function and its HTTP method(s). The handler is an ordinary FastAPI endpoint, so
path/query/body parameters and `Depends` work as usual:

	async def get_order(order_id: str, db: Annotated[Db, Depends(get_db)]) -> Order:
		...

	ServerBuilder(settings).with_routes(
		Route("/orders/{order_id}", get_order, "GET", requires_auth=True),
		Route("/orders", create_order, "POST", status_code=201, dependencies=[Depends(audit)]),
	)
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from fastapi import APIRouter
from fastapi.params import Depends

from app.core.settings import Settings

_ROUTE_ATTR = "__route_specs__"

# What ServerBuilder.with_routes accepts.
type RouteSource = type[Routes] | Routes | Route | APIRouter


@dataclass(frozen=True, slots=True)
class RouteSpec:
	path: str
	methods: tuple[str, ...]
	kwargs: dict[str, Any] = field(default_factory=dict)


def route(path: str, *, methods: Sequence[str], **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
	"""
	Decorates a function to define it as a route handler with specific path, HTTP methods,
	and additional keyword arguments. The decorated function is augmented with metadata
	about the route, allowing a framework to register it as part of the application's
	routing mechanism.

	:param path: The URL path for the route.
	:type path: str
	:param methods: A sequence of HTTP methods (e.g., "GET", "POST") allowed for this route.
	:type methods: list[str] or tuple[str, ...]
	:param kwargs: Additional keyword arguments to specify configuration or attributes for the route.
	:type kwargs: dict
	:return: A decorator that associates the provided route metadata with the target function.
	:rtype: (Callable[[Callable[..., Any]], Callable[..., Any]])
	"""
	def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
		specs: list[RouteSpec] = getattr(func, _ROUTE_ATTR, [])
		specs.append(RouteSpec(path, tuple(m.upper() for m in methods), kwargs))
		setattr(func, _ROUTE_ATTR, specs)
		return func

	return decorator


def get(path: str, **kwargs: Any):
	return route(path, methods=["GET"], **kwargs)


def post(path: str, **kwargs: Any):
	return route(path, methods=["POST"], **kwargs)


def put(path: str, **kwargs: Any):
	return route(path, methods=["PUT"], **kwargs)


def patch(path: str, **kwargs: Any):
	return route(path, methods=["PATCH"], **kwargs)


def delete(path: str, **kwargs: Any):
	return route(path, methods=["DELETE"], **kwargs)


class Routes:
	prefix: ClassVar[str] = ""
	tags: ClassVar[list[str]] = []
	# Applied to every route in the class, e.g. [Depends(require_api_key)].
	dependencies: ClassVar[Sequence[Depends]] = ()
	# Require the same bearer credentials as the MCP endpoint (from ServerBuilder.with_auth).
	requires_auth: ClassVar[bool] = False

	def __init__(self, settings: Settings) -> None:
		self.settings = settings

	def router(self) -> APIRouter:
		"""
		Generates and returns an instance of FastAPI's APIRouter object configured with
		the routes and metadata defined in the class. The method dynamically generates
		the necessary routes based on the internal routing specifications, applies
		prefixes, tags, and dependencies, and then binds them to the router instance.

		The router object is designed to represent a collection of API routes with shared
		settings and methods, forming the backbone of the API structure in the application.

		:return: A configured instance of FastAPI's APIRouter containing all routes and
		    metadata defined in the class.
		:rtype: APIRouter
		"""
		router = APIRouter(prefix=self.prefix, tags=list(self.tags), dependencies=list(self.dependencies))
		for name, specs in self._route_methods():
			endpoint = getattr(self, name)
			for spec in specs:
				router.add_api_route(spec.path, endpoint, methods=list(spec.methods), **spec.kwargs)
		return router

	@classmethod
	def _route_methods(cls) -> list[tuple[str, list[RouteSpec]]]:
		"""
		Determines and aggregates route specifications defined in the class and its
		base classes. Walks the class's Method Resolution Order (MRO) in reverse
		order, ensuring that base classes are considered first. This allows subclasses
		to override methods and maintain definitions in order of appearance. Methods
		with route specifications are identified by the presence of a specific attribute.
		If a method is overridden and does not have route specifications, it will
		be excluded from the results.

		:return: A list of tuples where each tuple contains the name of a method and
		    a list of route specifications associated with that method.
		:rtype: list[tuple[str, list[RouteSpec]]]
		"""
		# Walk the MRO base-first so subclasses can override, keeping definition order.
		found: dict[str, list[RouteSpec]] = {}
		for klass in reversed(cls.__mro__):
			for name, attr in vars(klass).items():
				if specs := getattr(attr, _ROUTE_ATTR, None):
					found[name] = specs
				elif name in found:
					del found[name]
		return list(found.items())


class Route:
	"""
	One endpoint from a plain function, for when a :class:`Routes` class is more than you need.

	Not to be confused with Starlette's ``Route``: this one registers a FastAPI endpoint, so the
	handler gets FastAPI's request parsing, validation, ``Depends`` and OpenAPI docs.

	:param path: URL path, e.g. ``"/orders/{order_id}"``.
	:type path: str
	:param endpoint: The handler, sync or async, written like any FastAPI endpoint.
	:type endpoint: Callable[..., Any]
	:param methods: HTTP method or methods, e.g. ``"POST"`` or ``["GET", "HEAD"]``.
	:type methods: str | Sequence[str]
	:param requires_auth: Require the same bearer credentials as the MCP endpoint.
	:type requires_auth: bool
	:param options: Forwarded to ``APIRouter.add_api_route``: ``dependencies``, ``response_model``,
	    ``status_code``, ``summary``, ``tags``, ``response_class``, ...
	"""

	def __init__(
		self,
		path: str,
		endpoint: Callable[..., Any],
		methods: str | Sequence[str] = "GET",
		*,
		requires_auth: bool = False,
		**options: Any,
	) -> None:
		methods = (methods,) if isinstance(methods, str) else tuple(methods)
		if not methods:
			raise ValueError(f"Route {path!r} needs at least one HTTP method")
		self.path = path
		self.endpoint = endpoint
		self.methods = tuple(m.upper() for m in methods)
		self.requires_auth = requires_auth
		self.options = options

	def router(self) -> APIRouter:
		"""
		:return: A router holding just this endpoint.
		:rtype: APIRouter
		"""
		router = APIRouter()
		router.add_api_route(self.path, self.endpoint, methods=list(self.methods), **self.options)
		return router

	def __repr__(self) -> str:
		name = getattr(self.endpoint, "__name__", repr(self.endpoint))
		return f"Route({'|'.join(self.methods)} {self.path} -> {name})"
