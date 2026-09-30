import pytest
from pydantic import ValidationError

from app.core.settings import Environment, McpAuthSettings, ServerSettings
from tests.conftest import IsolatedSettings as Settings


def test_defaults_to_console_logs_and_docs_in_development():
	settings = Settings()
	assert settings.environment is Environment.DEVELOPMENT
	assert settings.log_format == "console"
	assert settings.show_docs


def test_production_defaults_to_json_logs_and_no_docs():
	settings = Settings(environment=Environment.PRODUCTION)
	assert settings.log_format == "json"
	assert not settings.show_docs


def test_nested_env_vars(monkeypatch):
	monkeypatch.setenv("APP_SERVER__PORT", "9000")
	monkeypatch.setenv("APP_MCP__AUTH__MODE", "static")
	monkeypatch.setenv("APP_MCP__AUTH__TOKENS", '["abc"]')
	settings = Settings()
	assert settings.server.port == 9000
	assert settings.mcp.auth.mode == "static"
	assert settings.mcp.auth.tokens[0].get_secret_value() == "abc"


@pytest.mark.parametrize("overrides", [{"debug": True}, {"server": ServerSettings(reload=True)}])
def test_production_guardrails(overrides):
	with pytest.raises(ValidationError):
		Settings(environment=Environment.PRODUCTION, **overrides)


def test_reload_and_workers_are_exclusive():
	with pytest.raises(ValidationError):
		Settings(server=ServerSettings(reload=True, workers=2))


@pytest.mark.parametrize(
	"auth",
	[
		{"mode": "static"},
		{"mode": "jwt", "audience": "x"},
		{"mode": "jwt", "audience": "x", "jwks_uri": "https://idp/jwks", "public_key": "k"},
		{"mode": "jwt", "jwks_uri": "https://idp/jwks"},
		{"mode": "jwt", "audience": "x", "jwks_uri": "https://idp/jwks", "authorization_servers": ["https://idp"]},
	],
)
def test_invalid_mcp_auth(auth):
	with pytest.raises(ValidationError):
		McpAuthSettings(**auth)
