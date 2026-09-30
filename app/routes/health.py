from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio
from fastapi import Response
from pydantic import BaseModel

from app.core.logging import get_logger
from app.core.settings import Settings
from app.routes.base import Routes, get

log = get_logger("app.health")

# Raises when its dependency is unavailable.
type ReadinessCheck = Callable[[], Awaitable[Any]]

_CHECK_TIMEOUT_SECONDS = 3


class HealthStatus(BaseModel):
	status: str
	name: str
	version: str
	environment: str
	checks: dict[str, str] = {}


class HealthRoutes(Routes):
	"""
	Handles health check endpoints for liveness and readiness probes.

	Provides a set of endpoints to monitor the health status of the application.
	Useful for application lifecycle monitoring and service orchestration tools.

	:ivar prefix: The API route prefix for health-related endpoints.
	:type prefix: str
	:ivar tags: A list of tags associated with the health-related endpoints.
	:type tags: list[str]
	:param settings: Application settings.
	:type settings: Settings
	:param checks: Named readiness checks, e.g. ``{"mongo": database.ping}``. Each raises when
	    its dependency is unavailable; any failure (or a check taking over 3 seconds) makes
	    ``/health/ready`` return 503. Failure details go to the logs, not the response.
	:type checks: Mapping[str, ReadinessCheck] | None
	"""

	prefix = "/health"
	tags = ["health"]

	def __init__(self, settings: Settings, checks: Mapping[str, ReadinessCheck] | None = None) -> None:
		super().__init__(settings)
		self.checks = dict(checks or {})

	@get("/live", summary="Liveness probe")
	async def live(self) -> dict[str, str]:
		"""
		Handles the liveness probe endpoint to verify the application's running status.

		This endpoint is typically used by health checking mechanisms to ensure that
		the service is operational. It returns a simple status message indicating the
		success of the liveness check.

		:return: A dictionary containing the liveness status.
		:rtype: dict[str, str]
		"""
		return {"status": "ok"}

	@get("/ready", summary="Readiness probe", responses={503: {"model": HealthStatus}})
	async def ready(self, response: Response) -> HealthStatus:
		"""
		Readiness probe endpoint that performs dependency checks and returns the current
		health status of the application. This checks the readiness of critical components
		like databases, caches, or upstream APIs, and provides the readiness status.

		:return: The health status of the application, including its readiness status, name,
		    version, and environment.
		:rtype: HealthStatus
		"""
		results = await self._run_checks()
		ready = all(result == "ok" for result in results.values())
		if not ready:
			response.status_code = 503
		return HealthStatus(
			status="ok" if ready else "unavailable",
			name=self.settings.name,
			version=self.settings.version,
			environment=self.settings.environment,
			checks=results,
		)

	async def _run_checks(self) -> dict[str, str]:
		results: dict[str, str] = {}

		async def run(name: str, check: ReadinessCheck) -> None:
			try:
				with anyio.fail_after(_CHECK_TIMEOUT_SECONDS):
					await check()
				results[name] = "ok"
			except Exception as exc:
				log.warning("health.check_failed", check=name, error=repr(exc))
				results[name] = "unavailable"

		async with anyio.create_task_group() as group:
			for name, check in self.checks.items():
				group.start_soon(run, name, check)
		return dict(sorted(results.items()))
