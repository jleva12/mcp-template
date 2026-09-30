class ServiceError(Exception):
	"""Base for errors caused by the caller's input. Messages are safe to show to clients."""


class NotFoundError(ServiceError):
	pass


class InvalidTemplateError(ServiceError):
	"""The template or its JSON Schema can't be accepted (syntax, size, encoding)."""


class InvalidDataError(ServiceError):
	"""
	The render data doesn't fit the template.

	:param message: Summary of the problem.
	:type message: str
	:param errors: One entry per failing field, e.g. ``customer.name: 'name' is a required property``.
	:type errors: list[str] | None
	"""

	def __init__(self, message: str, errors: list[str] | None = None) -> None:
		super().__init__(message)
		self.errors = errors or []


class RenderError(ServiceError):
	"""The template failed while rendering (runtime error, unsafe operation, timeout)."""


class UnsupportedFormatError(ServiceError):
	"""No renderer is registered for the requested output format."""
