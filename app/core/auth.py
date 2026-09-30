"""Authentication for the MCP endpoint.

Auth is injected through ``ServerBuilder.with_auth``, which takes any FastMCP
``AuthProvider``:

- ``auth_from_settings(settings)``: API keys or JWTs, chosen by ``APP_MCP__AUTH__MODE``
- an OAuth provider (``GitHubProvider``, ``GoogleProvider``, ``OIDCProxy``, ``OAuthProxy``, ...)
  when people sign in interactively from clients like Claude or Cursor
- several at once, e.g. OAuth for people plus API keys for services; see :func:`combine_auth`

The same providers protect API route classes that set ``requires_auth = True`` (see
:func:`http_bearer_auth`).
"""

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.server.auth import AuthProvider, MultiAuth, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import SecretStr

from app.core.settings import Settings


class ApiKeyVerifier(TokenVerifier):
	"""
	Bearer API keys for trusted service-to-service callers.

	Keys are held only as SHA-256 digests and compared in constant time.

	:param keys: Accepted keys.
	:type keys: Sequence[SecretStr]
	:param required_scopes: Scopes granted to (and required of) every key.
	:type required_scopes: list[str] | None
	"""

	def __init__(self, keys: Sequence[SecretStr], required_scopes: list[str] | None = None) -> None:
		super().__init__(required_scopes=required_scopes)
		self._digests = [self._digest(key.get_secret_value()) for key in keys]

	@staticmethod
	def _digest(value: str) -> bytes:
		return hashlib.sha256(value.encode()).digest()

	async def verify_token(self, token: str) -> AccessToken | None:
		digest = self._digest(token)
		for index, known in enumerate(self._digests):
			if hmac.compare_digest(digest, known):
				return AccessToken(token=token, client_id=f"api-key-{index}", scopes=list(self.required_scopes or []))
		return None


def auth_from_settings(settings: Settings) -> AuthProvider | None:
	"""
	Builds the provider selected by ``settings.mcp.auth.mode``.

	:param settings: Application settings.
	:type settings: Settings
	:return: ``None`` for mode ``none``, an :class:`ApiKeyVerifier` for ``static``, or a
	    ``JWTVerifier`` (wrapped in ``RemoteAuthProvider`` when authorization servers are set) for ``jwt``.
	:rtype: AuthProvider | None
	"""
	auth = settings.mcp.auth
	scopes = auth.required_scopes or None
	match auth.mode:
		case "static":
			return ApiKeyVerifier(auth.tokens, required_scopes=scopes)
		case "jwt":
			verifier = JWTVerifier(
				jwks_uri=auth.jwks_uri,
				public_key=auth.public_key.get_secret_value() if auth.public_key else None,
				issuer=auth.issuer,
				audience=auth.audience,
				algorithm=auth.algorithm,
				required_scopes=scopes,
				base_url=auth.base_url,
			)
			if not (auth.authorization_servers and auth.base_url):
				return verifier
			return RemoteAuthProvider(
				token_verifier=verifier,
				# Passed as str on purpose: AnyHttpUrl would append "/" and alter the issuer URLs.
				authorization_servers=auth.authorization_servers,  # pyright: ignore[reportArgumentType]
				base_url=auth.base_url,
				resource_name=settings.mcp.name,
			)
		case _:
			return None


def combine_auth(providers: Sequence[AuthProvider], required_scopes: list[str] | None = None) -> AuthProvider | None:
	"""
	Combines providers so a request is accepted if any of them accepts its token.

	At most one provider may own OAuth routes and metadata (an OAuth proxy or a
	``RemoteAuthProvider``); every other provider must be a plain ``TokenVerifier``
	(``JWTVerifier``, :class:`ApiKeyVerifier`, ...). The OAuth provider is tried first.

	By default every token must carry the OAuth provider's required scopes (e.g. GitHub's
	``user``), which API keys and JWTs from another issuer usually don't. Set
	``required_scopes`` to change that, e.g. ``[]``; ``JWTVerifier`` still enforces its own.

	:param providers: Providers to accept tokens from.
	:type providers: Sequence[AuthProvider]
	:param required_scopes: Scopes every token must carry, overriding the OAuth provider's.
	:type required_scopes: list[str] | None
	:return: ``None`` for no providers, the provider itself for one, else a ``MultiAuth``.
	:rtype: AuthProvider | None
	:raises ValueError: If more than one provider owns OAuth routes.
	"""
	if len(providers) <= 1:
		return providers[0] if providers else None
	servers = [p for p in providers if not isinstance(p, TokenVerifier)]
	verifiers = [p for p in providers if isinstance(p, TokenVerifier)]
	if len(servers) > 1:
		names = ", ".join(type(p).__name__ for p in servers)
		raise ValueError(f"only one auth provider may own OAuth routes; got {names}")
	return MultiAuth(server=servers[0] if servers else None, verifiers=verifiers, required_scopes=required_scopes)


def http_bearer_auth(provider: AuthProvider) -> Callable[..., Awaitable[AccessToken]]:
	"""
	FastAPI dependency that authenticates API routes with the MCP endpoint's auth provider.

	The same tokens (API keys, JWTs, OAuth-issued tokens) work for both. ``ServerBuilder``
	applies it to route classes with ``requires_auth = True``.

	:param provider: The combined auth provider from ``ServerBuilder.with_auth``.
	:type provider: AuthProvider
	:return: A dependency returning the caller's verified access token.
	:rtype: Callable[..., Awaitable[AccessToken]]
	"""
	scheme = HTTPBearer(auto_error=False, description="Same bearer token as the MCP endpoint")

	async def authenticate(
		credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(scheme)],
	) -> AccessToken:
		challenge = {"WWW-Authenticate": "Bearer"}
		if credentials is None:
			raise HTTPException(status_code=401, detail="Missing bearer token", headers=challenge)
		token = await provider.verify_token(credentials.credentials)
		if token is None:
			raise HTTPException(status_code=401, detail="Invalid or expired token", headers=challenge)
		if missing := set(provider.required_scopes or []) - set(token.scopes):
			raise HTTPException(
				status_code=403,
				detail=f"Missing required scopes: {', '.join(sorted(missing))}",
				headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
			)
		return token

	return authenticate
