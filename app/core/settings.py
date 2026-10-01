"""Application settings, loaded from environment variables and an optional `.env` file.

Every field maps to an env var with the `APP_` prefix; nested groups use `__`:

	APP_ENVIRONMENT=production
	APP_SERVER__PORT=8080
	APP_LOGGING__LEVEL=DEBUG
	APP_MCP__AUTH__MODE=jwt

See `.env.example` for the full list.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
	DEVELOPMENT = "development"
	TEST = "test"
	STAGING = "staging"
	PRODUCTION = "production"


class ServerSettings(BaseModel):
	"""Uvicorn process settings."""

	host: str = "127.0.0.1"
	port: int = Field(default=8000, ge=1, le=65535)
	workers: int = Field(default=1, ge=1)
	reload: bool = False
	# Trust X-Forwarded-* headers from these proxy IPs ("*" = any; only behind a trusted LB).
	proxy_headers: bool = True
	forwarded_allow_ips: str = "127.0.0.1"
	# Mount prefix when served behind a path-rewriting proxy (e.g. "/api").
	root_path: str = ""
	timeout_keep_alive: int = Field(default=5, ge=1)
	# MCP holds long-lived SSE streams open; bound how long shutdown waits on them.
	timeout_graceful_shutdown: int = Field(default=30, ge=1)
	limit_concurrency: int | None = Field(default=None, ge=1)


class LoggingSettings(BaseModel):
	level: LogLevel = "INFO"
	# None picks by environment: "console" in development/test, "json" everywhere else.
	format: Literal["json", "console"] | None = None
	access_log: bool = True
	# Paths excluded from access logs (health probes are noisy).
	access_log_exclude_paths: list[str] = ["/health/live", "/health/ready"]
	# Per-logger level overrides for noisy third-party libraries.
	levels: dict[str, LogLevel] = {
		"mcp.server.lowlevel.server": "WARNING",
		"mcp.server.streamable_http": "WARNING",
		"mcp.server.streamable_http_manager": "WARNING",
		"sse_starlette": "WARNING",
		"weasyprint.progress": "WARNING",
		"httpx": "WARNING",
		"httpx2": "WARNING",
		"httpcore": "WARNING",
	}


class CorsSettings(BaseModel):
	"""CORS is only installed when `allow_origins` is non-empty."""

	allow_origins: list[str] = []
	allow_credentials: bool = False
	allow_methods: list[str] = ["GET", "POST", "DELETE", "OPTIONS"]
	allow_headers: list[str] = [
		"Authorization",
		"Content-Type",
		"X-Request-ID",
		"Mcp-Session-Id",
		"Mcp-Protocol-Version",
		"Last-Event-ID",
	]
	# Browser MCP clients must be able to read the session id header.
	expose_headers: list[str] = ["X-Request-ID", "Mcp-Session-Id"]
	max_age: int = 600


class McpAuthSettings(BaseModel):
	"""Bearer-token auth for the MCP endpoint.

	- none:   no auth (development, or when a gateway in front handles it)
	- static: fixed API keys, for service-to-service callers you control
	- jwt:    verify JWTs from an identity provider via JWKS URI or public key
	- oauth:  people sign in with GitHub, Google or any OIDC provider from MCP clients
	          like Claude, Cursor or VS Code; this server runs the OAuth flow
	"""

	mode: Literal["none", "static", "jwt", "oauth"] = "none"
	# For oauth, also the scopes requested at sign-in. Empty = the provider's default.
	required_scopes: list[str] = []

	# static
	tokens: list[SecretStr] = []

	# jwt
	jwks_uri: str | None = None
	# PEM public key (RS*/ES*/PS*) or shared secret (HS*).
	public_key: SecretStr | None = None
	issuer: str | None = None
	audience: str | None = None
	algorithm: str = "RS256"
	# Issuers of valid tokens. When set, the server publishes OAuth protected-resource
	# metadata (RFC 9728) at /.well-known/oauth-protected-resource<mcp.path> so MCP
	# clients can discover where to log in. Requires base_url.
	authorization_servers: list[str] = []
	# Public URL of this server as clients reach it, e.g. https://mcp.example.com
	base_url: str | None = None

	# oauth: register an OAuth app with the provider, with callback URL <base_url>/auth/callback.
	# Also needs base_url. For oidc, audience (above) is forwarded to IdPs that need one, e.g. Auth0.
	provider: Literal["github", "google", "oidc"] = "github"
	client_id: str | None = None
	client_secret: SecretStr | None = None
	# oidc: the IdP's discovery document, e.g. https://tenant.auth0.com/.well-known/openid-configuration
	config_url: str | None = None
	# Signs the tokens this server issues to MCP clients and encrypts stored upstream tokens.
	# Same on every replica; changing it signs everyone out. Unset = derived from client_secret.
	jwt_signing_key: SecretStr | None = None
	# Client registrations and upstream tokens: mongo (survives restarts, shared by replicas),
	# or memory (development/tests only; lost on restart and not shared between workers).
	storage: Literal["mongo", "memory"] = "mongo"
	# Who may sign in: GitHub usernames or verified emails, and verified email domains.
	# Both empty = anyone with an account at the provider.
	allowed_users: list[str] = []
	allowed_domains: list[str] = []

	@model_validator(mode="after")
	def _check_mode(self) -> Self:
		if self.mode == "static" and not self.tokens:
			raise ValueError("mcp.auth.mode=static requires at least one token in mcp.auth.tokens")
		if self.mode == "jwt":
			if bool(self.jwks_uri) == bool(self.public_key):
				raise ValueError("mcp.auth.mode=jwt requires exactly one of jwks_uri or public_key")
			if not self.audience:
				raise ValueError("mcp.auth.mode=jwt requires an audience")
			if self.authorization_servers and not self.base_url:
				raise ValueError("mcp.auth.authorization_servers requires base_url")
		if self.mode == "oauth":
			if not (self.client_id and self.client_secret):
				raise ValueError("mcp.auth.mode=oauth requires client_id and client_secret")
			if not self.base_url:
				raise ValueError("mcp.auth.mode=oauth requires base_url, this server's public URL")
			if self.provider == "oidc" and not self.config_url:
				raise ValueError("mcp.auth.provider=oidc requires config_url")
		elif self.allowed_users or self.allowed_domains:
			raise ValueError("mcp.auth.allowed_users and allowed_domains require mode=oauth")
		return self


class McpRateLimitSettings(BaseModel):
	enabled: bool = False
	requests_per_second: float = Field(default=10.0, gt=0)
	burst_capacity: int | None = Field(default=None, ge=1)


class McpSettings(BaseModel):
	enabled: bool = True
	name: str = "template-mcp"
	# Sent to clients on connect; tells the model how to use this server's tools.
	instructions: str | None = (
		"Generates PDF and Word (DOCX) documents from Jinja HTML templates. "
		"Create a template once with create_template (a single HTML file with CSS inlined in <style> "
		"and images as data: URIs, optionally with a JSON Schema for its data), then call "
		"render_document with the template_id, its variables as template_data, and output_format "
		'"pdf" (default) or "docx" to get a document_id and a download '
		"link. Use get_template to see a template's schema before rendering, add_template_version to "
		"change a template, and get_document for a fresh download link."
	)
	path: str = "/mcp"
	# Stateless mode: no server-side sessions, so any worker/replica can serve any request.
	# Required for horizontal scaling behind a load balancer without sticky sessions.
	stateless_http: bool = False
	# Return plain JSON instead of SSE streams (only meaningful in stateless mode).
	json_response: bool = False
	# Hide internal exception details from clients; only ToolError messages are sent.
	mask_error_details: bool = True
	strict_input_validation: bool = False
	# Idle stateful sessions are reaped after this many seconds (ignored when stateless).
	session_idle_timeout: float | None = Field(default=3600, gt=0)
	# DNS-rebinding protection: validate Host/Origin headers.
	host_origin_protection: bool | Literal["auto"] = "auto"
	allowed_hosts: list[str] = []
	allowed_origins: list[str] = []
	auth: McpAuthSettings = McpAuthSettings()
	rate_limit: McpRateLimitSettings = McpRateLimitSettings()


class FileSettings(BaseModel):
	"""Storage for generated files and their signed download links (see app/core/files.py).

	- local: files on this server's disk; for development or a single instance with a volume
	- s3:    any S3-compatible store (AWS S3, Cloudflare R2, MinIO). Credentials come from the
	         standard AWS env vars (AWS_ACCESS_KEY_ID, ...) or the instance's IAM role.
	"""

	backend: Literal["local", "s3"] = "local"
	# HMAC key for download links. Required outside development/test, and must be the same
	# on every replica. Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
	signing_key: SecretStr | None = None
	link_ttl_seconds: int = Field(default=3600, gt=0)
	# Origin used in links, e.g. https://mcp.example.com. Unset = taken from the incoming request.
	public_base_url: str | None = None

	local_dir: Path = Path(".data/files")

	s3_bucket: str | None = None
	s3_prefix: str = "files/"
	s3_region: str | None = None
	# For R2/MinIO, e.g. https://<account>.r2.cloudflarestorage.com
	s3_endpoint_url: str | None = None
	# The /files route redirects to a storage URL valid this long; it's followed immediately.
	s3_presign_ttl_seconds: int = Field(default=60, gt=0)

	@model_validator(mode="after")
	def _check_backend(self) -> Self:
		if self.backend == "s3" and not self.s3_bucket:
			raise ValueError("files.backend=s3 requires files.s3_bucket")
		return self


class MongoSettings(BaseModel):
	# Include credentials in the URL, e.g. mongodb+srv://user:pass@cluster.example.net
	url: SecretStr = SecretStr("mongodb://localhost:27017")
	database: str = "template_mcp"
	server_selection_timeout_ms: int = Field(default=5000, gt=0)


class TemplateSettings(BaseModel):
	"""Limits for uploaded templates and rendering."""

	# Templates are stored in MongoDB, whose documents are capped at 16 MB.
	max_template_bytes: int = Field(default=5 * 1024 * 1024, gt=0, le=15 * 1024 * 1024)
	max_schema_bytes: int = Field(default=256 * 1024, gt=0)
	max_data_bytes: int = Field(default=1024 * 1024, gt=0)
	render_timeout_seconds: float = Field(default=60, gt=0)
	# Documents rendered at once per output format per worker process. Rendering is CPU-bound
	# (and WeasyPrint isn't documented as thread-safe), so scale with server.workers rather than raising this.
	render_concurrency: int = Field(default=1, ge=1)


class Settings(BaseSettings):
	model_config = SettingsConfigDict(
		env_prefix="APP_",
		env_nested_delimiter="__",
		env_file=".env",
		env_file_encoding="utf-8",
		extra="ignore",
	)

	name: str = "template-mcp"
	version: str = "0.1.0"
	environment: Environment = Environment.DEVELOPMENT
	debug: bool = False
	# None → enabled everywhere except production.
	docs_enabled: bool | None = None

	server: ServerSettings = ServerSettings()
	logging: LoggingSettings = LoggingSettings()
	cors: CorsSettings = CorsSettings()
	mcp: McpSettings = McpSettings()
	files: FileSettings = FileSettings()
	mongo: MongoSettings = MongoSettings()
	templates: TemplateSettings = TemplateSettings()

	@property
	def is_production(self) -> bool:
		return self.environment is Environment.PRODUCTION

	@property
	def log_format(self) -> Literal["json", "console"]:
		if self.logging.format:
			return self.logging.format
		return "console" if self.environment in (Environment.DEVELOPMENT, Environment.TEST) else "json"

	@property
	def show_docs(self) -> bool:
		return self.docs_enabled if self.docs_enabled is not None else not self.is_production

	@model_validator(mode="after")
	def _production_guardrails(self) -> Self:
		if self.is_production:
			if self.debug:
				raise ValueError("debug must be disabled in production")
			if self.server.reload:
				raise ValueError("server.reload must be disabled in production")
			if "*" in self.cors.allow_origins and self.cors.allow_credentials:
				raise ValueError("cors: wildcard origins cannot be combined with credentials")
		if self.server.reload and self.server.workers > 1:
			raise ValueError("server.reload and server.workers > 1 are mutually exclusive")
		auth = self.mcp.auth
		if auth.mode == "oauth" and auth.storage == "memory" and self.server.workers > 1:
			# A sign-in started on one worker can finish on another, which wouldn't know about it.
			raise ValueError("mcp.auth.storage=memory can't be shared by server.workers > 1; use mongo")
		return self


@lru_cache
def get_settings() -> Settings:
	return Settings()
