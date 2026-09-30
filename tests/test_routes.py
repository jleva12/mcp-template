from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.routes import HealthRoutes, Routes, get, post
from app.server import ServerBuilder
from tests.conftest import make_settings


class BoomRoutes(Routes):
	prefix = "/boom"

	@get("/")
	async def boom(self) -> dict:
		raise RuntimeError("kaboom")

	@get("/teapot")
	async def teapot(self) -> dict:
		raise HTTPException(status_code=418, detail="short and stout")


def test_health(client):
	assert client.get("/health/live").json() == {"status": "ok"}
	ready = client.get("/health/ready").json()
	assert ready["status"] == "ok"
	assert ready["environment"] == "test"


def test_hello(client):
	assert client.get("/").json() == {"message": "Hello World"}
	assert client.get("/hello/Joe").json() == {"message": "Hello Joe"}


def test_unknown_path_and_wrong_method_stay_fastapi_json(client):
	# The MCP app is mounted at the root; it must not swallow FastAPI's 404/405s.
	missing = client.get("/does-not-exist")
	assert missing.status_code == 404
	assert missing.json() == {"detail": "Not Found"}
	assert client.post("/health/live").status_code == 405


def test_request_id_generated_and_echoed(client):
	generated = client.get("/health/live").headers["X-Request-ID"]
	assert len(generated) == 32

	assert client.get("/health/live", headers={"X-Request-ID": "trace-123"}).headers["X-Request-ID"] == "trace-123"
	# Values that could inject into logs are replaced.
	assert client.get("/health/live", headers={"X-Request-ID": "bad id\n"}).headers["X-Request-ID"] != "bad id\n"


def test_unhandled_exception_returns_json_500_with_request_id(make_client):
	client = make_client(routes=(BoomRoutes,))
	response = client.get("/boom/", headers={"X-Request-ID": "req-1"})
	assert response.status_code == 500
	assert response.json() == {"detail": "Internal Server Error", "request_id": "req-1"}
	assert response.headers["X-Request-ID"] == "req-1"


def test_http_exceptions_pass_through(make_client):
	client = make_client(routes=(BoomRoutes,))
	response = client.get("/boom/teapot")
	assert response.status_code == 418
	assert response.json() == {"detail": "short and stout"}


def test_docs_disabled_in_production(make_client):
	client = make_client(make_settings(environment="production"))
	assert client.get("/docs").status_code == 404
	assert client.get("/openapi.json").status_code == 404


def test_routes_register_in_definition_order_and_respect_overrides():
	class Base(Routes):
		@get("/items/me")
		async def me(self) -> dict:
			return {}

		@get("/items/{item_id}")
		async def item(self, item_id: int) -> dict:
			return {}

		@post("/items")
		async def create(self) -> dict:
			return {}

	class Child(Base):
		# Overriding without a decorator removes the route.
		async def create(self) -> dict:
			return {}

	paths = [route.path for route in Child(make_settings()).router().routes if isinstance(route, APIRoute)]
	assert paths == ["/items/me", "/items/{item_id}"]


def test_readiness_reports_failing_checks():
	async def healthy() -> None:
		return None

	async def broken() -> None:
		raise ConnectionError("database down")

	settings = make_settings()
	app = ServerBuilder(settings).with_routes(HealthRoutes(settings, checks={"db": broken, "cache": healthy})).build()
	with TestClient(app) as client:
		response = client.get("/health/ready")
	assert response.status_code == 503
	assert response.json()["status"] == "unavailable"
	assert response.json()["checks"] == {"cache": "ok", "db": "unavailable"}
