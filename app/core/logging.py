"""Structured logging for the app, uvicorn, FastAPI and FastMCP.

Everything is routed through the stdlib root logger and rendered by structlog, so
third-party and application logs share one format: JSON lines in production (one
object per line, ready for Datadog/Loki/CloudWatch), colored key/value output in
development. Values bound with `structlog.contextvars` (e.g. `request_id`) are
attached to every line, including lines emitted by libraries.

Usage:
	from app.core.logging import get_logger
	log = get_logger(__name__)
	log.info("order.created", order_id=order.id)
"""

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor

from app.core.settings import Settings

# Loggers that install their own handlers; they're reset to propagate to root.
_MANAGED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "fastmcp", "mcp")


def configure_logging(settings: Settings) -> None:
	json_logs = settings.log_format == "json"

	shared: list[Processor] = [
		structlog.contextvars.merge_contextvars,
		structlog.stdlib.add_log_level,
		structlog.stdlib.add_logger_name,
		structlog.processors.TimeStamper(fmt="iso", utc=True),
		structlog.processors.StackInfoRenderer(),
	]
	if json_logs:
		shared.append(
			structlog.processors.CallsiteParameterAdder(
				{structlog.processors.CallsiteParameter.MODULE, structlog.processors.CallsiteParameter.LINENO},
			)
		)
	# Stdlib records (uvicorn, fastmcp, ...) also carry any `extra={...}` fields.
	foreign_pre_chain: list[Processor] = [*shared, structlog.stdlib.ExtraAdder(), _drop_color_message]

	structlog.configure(
		processors=[
			structlog.stdlib.filter_by_level,
			*shared,
			structlog.stdlib.PositionalArgumentsFormatter(),
			structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
		],
		logger_factory=structlog.stdlib.LoggerFactory(),
		wrapper_class=structlog.stdlib.BoundLogger,
		cache_logger_on_first_use=True,
	)

	final: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
	if json_logs:
		final += [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
	else:
		final.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))

	formatter = structlog.stdlib.ProcessorFormatter(foreign_pre_chain=foreign_pre_chain, processors=final)
	handler = logging.StreamHandler(sys.stdout)
	handler.setFormatter(formatter)

	root = logging.getLogger()
	root.handlers.clear()
	root.addHandler(handler)
	root.setLevel(settings.logging.level)

	for name in _MANAGED_LOGGERS:
		logger = logging.getLogger(name)
		logger.handlers.clear()
		logger.propagate = True
		logger.setLevel(logging.NOTSET)
	# Access lines come from RequestContextMiddleware (with request_id and duration).
	logging.getLogger("uvicorn.access").disabled = True

	for name, level in settings.logging.levels.items():
		logging.getLogger(name).setLevel(level)

	logging.captureWarnings(True)


def _drop_color_message(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
	# Uvicorn attaches an ANSI-colored duplicate of each message as `extra`.
	event_dict.pop("color_message", None)
	return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
	return structlog.stdlib.get_logger(name)
