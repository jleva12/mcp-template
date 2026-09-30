"""MongoDB documents for templates, their versions, and the documents rendered from them.

IDs are random (``tpl_`` / ``doc_`` + 128 bits) rather than ObjectIds: anyone holding an ID
can use it, so IDs must not be guessable. ObjectIds encode a timestamp and counter.
"""

import secrets
from datetime import UTC, datetime
from enum import StrEnum

from beanie import Document, Indexed
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel


class OutputFormat(StrEnum):
	"""File types a template can be rendered to; each has a renderer registered in ``main.py``."""

	PDF = "pdf"
	DOCX = "docx"


def _new_id(prefix: str) -> str:
	return f"{prefix}_{secrets.token_hex(16)}"


def _now() -> datetime:
	return datetime.now(UTC)


class Template(Document):
	"""A named template; its content lives in numbered :class:`TemplateVersion` documents."""

	id: str = Field(default_factory=lambda: _new_id("tpl"))  # pyright: ignore[reportIncompatibleVariableOverride]
	name: str
	description: str | None = None
	latest_version: int = 0
	created_at: datetime = Field(default_factory=_now)
	updated_at: datetime = Field(default_factory=_now)

	class Settings:
		name = "templates"


class TemplateVersion(Document):
	"""
	One immutable revision of a template.

	``json_schema`` is stored as JSON text: schema keywords such as ``$ref`` and ``$defs``
	aren't safe as MongoDB field names.
	"""

	id: str = Field(default_factory=lambda: _new_id("tplv"))  # pyright: ignore[reportIncompatibleVariableOverride]
	template_id: str
	version: int
	content: str
	json_schema: str | None = None
	sha256: str
	size_bytes: int
	created_at: datetime = Field(default_factory=_now)

	class Settings:
		name = "template_versions"
		indexes = [IndexModel([("template_id", ASCENDING), ("version", ASCENDING)], unique=True)]


class DocumentFile(BaseModel):
	"""Where the rendered file lives in the ``FileStore``."""

	file_id: str
	filename: str
	content_type: str
	size_bytes: int


class RenderedDocument(Document):
	"""A file rendered from a specific template version."""

	id: str = Field(default_factory=lambda: _new_id("doc"))  # pyright: ignore[reportIncompatibleVariableOverride]
	template_id: Indexed(str)  # type: ignore[valid-type]
	template_version: int
	# Documents stored before other formats existed have no format field; they're all PDFs.
	format: OutputFormat = OutputFormat.PDF
	file: DocumentFile
	# None for formats without fixed pages: Word lays out a DOCX when it opens it.
	page_count: int | None
	render_ms: float
	# Caller-supplied id for tracing this generation in their system and our logs.
	correlation_id: str | None = None
	created_at: datetime = Field(default_factory=_now)

	class Settings:
		name = "documents"


MODELS = [Template, TemplateVersion, RenderedDocument]
