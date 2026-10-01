"""Authentication for the MCP endpoint.

Auth is injected through ``ServerBuilder.with_auth``, which takes any FastMCP
``AuthProvider``:

- ``auth_from_settings(settings)``: API keys, JWTs, or OAuth sign-in with GitHub, Google or any
  OIDC provider, chosen by ``APP_MCP__AUTH__MODE``
- any other FastMCP OAuth provider built in code (``AzureProvider``, ``WorkOSProvider``, ``OAuthProxy``, ...),
  optionally limited to certain people with :class:`AllowedUsers`
- several at once, e.g. OAuth for people plus API keys for services; see :func:`combine_auth`

The same providers protect API route classes that set ``requires_auth = True`` (see
:func:`http_bearer_auth`).
"""

import hashlib
import hmac
import warnings
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp.server.auth import AuthProvider, MultiAuth, OIDCProxy, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.stores.mongodb import MongoDBStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from pydantic import SecretStr
from pymongo import AsyncMongoClient

from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger("app.auth")


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


class AllowedUsers(MultiAuth):
	"""
	Lets only the listed people through an OAuth provider; everyone else is refused as if their token were invalid.

	A person is allowed when their GitHub username or verified email is in ``users``, or their verified
	email's domain is exactly one of ``domains`` (case-insensitive). Refusals are logged as
	``mcp.auth.user_not_allowed``. Built on ``MultiAuth`` so the provider's OAuth routes and metadata
	are served unchanged.

	:param provider: The OAuth provider people sign in with, e.g. ``GitHubProvider``.
	:type provider: AuthProvider
	:param users: GitHub usernames or email addresses.
	:type users: Sequence[str]
	:param domains: Email domains, e.g. ``["example.com"]``.
	:type domains: Sequence[str]
	"""

	def __init__(self, provider: AuthProvider, *, users: Sequence[str] = (), domains: Sequence[str] = ()) -> None:
		super().__init__(server=provider)
		self.users = {user.strip().lower() for user in users} - {""}
		self.domains = {domain.strip().lower().removeprefix("@") for domain in domains} - {""}

	async def verify_token(self, token: str) -> AccessToken | None:
		access = await super().verify_token(token)
		if access is None or self.allows(access.claims):
			return access
		log.warning("mcp.auth.user_not_allowed", subject=access.subject, login=access.claims.get("login"))
		return None

	def allows(self, claims: dict[str, Any]) -> bool:
		"""
		:param claims: Verified token claims; GitHub's carry ``login``, and OIDC/Google's ``email_verified``.
		:type claims: dict[str, Any]
		:return: Whether the person these claims identify may sign in.
		:rtype: bool
		"""
		login = str(claims.get("login") or "").lower()
		email = str(claims.get("email") or "").lower()
		# GitHub (the only source of `login`) exposes only verified emails and has no email_verified
		# claim; anywhere else an email counts only when marked verified. Google sends "true" as a string.
		if str(claims.get("email_verified", bool(login))).lower() != "true":
			email = ""
		if login in self.users or email in self.users:
			return True
		_, at, domain = email.rpartition("@")
		return bool(at) and domain in self.domains


def auth_from_settings(settings: Settings) -> AuthProvider | None:
	"""
	Builds the provider selected by ``settings.mcp.auth.mode``.

	:param settings: Application settings.
	:type settings: Settings
	:return: ``None`` for mode ``none``, an :class:`ApiKeyVerifier` for ``static``, a ``JWTVerifier``
	    (wrapped in ``RemoteAuthProvider`` when authorization servers are set) for ``jwt``, or an OAuth
	    provider (wrapped in :class:`AllowedUsers` when sign-in is limited) for ``oauth``.
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
		case "oauth":
			provider = _oauth_provider(settings)
			if auth.allowed_users or auth.allowed_domains:
				return AllowedUsers(provider, users=auth.allowed_users, domains=auth.allowed_domains)
			if settings.is_production:
				log.warning("mcp.auth.oauth_open", detail=f"anyone with a {auth.provider} account can sign in")
			return provider
		case _:
			return None


def _oauth_provider(settings: Settings) -> AuthProvider:
	"""
	Builds the OAuth proxy for ``settings.mcp.auth.provider``.

	It serves the OAuth endpoints MCP clients use (discovery, dynamic client registration,
	``/authorize``, ``/token``) and sends people to the provider to sign in, with the provider
	redirecting back to ``<base_url>/auth/callback``.
	"""
	auth = settings.mcp.auth
	assert auth.client_id and auth.client_secret and auth.base_url  # checked by McpAuthSettings
	client_secret = auth.client_secret.get_secret_value()
	# Derived here, the same way FastMCP would, so the storage encryption key can come from it too.
	if auth.jwt_signing_key:
		signing_key = derive_jwt_key(
			low_entropy_material=auth.jwt_signing_key.get_secret_value(), salt="fastmcp-jwt-signing-key"
		)
	else:
		signing_key = derive_jwt_key(high_entropy_material=client_secret, salt="fastmcp-jwt-signing-key")
	storage = _oauth_storage(settings, signing_key)
	scopes = auth.required_scopes or None

	match auth.provider:
		case "github":
			return GitHubProvider(
				client_id=auth.client_id,
				client_secret=client_secret,
				base_url=auth.base_url,
				required_scopes=scopes,
				client_storage=storage,
				jwt_signing_key=signing_key,
				# Every request re-checks the GitHub token with two GitHub API calls; reuse the result briefly.
				cache_ttl_seconds=300,
			)
		case "google":
			return GoogleProvider(
				client_id=auth.client_id,
				client_secret=client_secret,
				base_url=auth.base_url,
				required_scopes=scopes or ["openid", "email"],
				client_storage=storage,
				jwt_signing_key=signing_key,
			)
		case "oidc":
			assert auth.config_url  # checked by McpAuthSettings
			return OIDCProxy(
				config_url=auth.config_url,
				client_id=auth.client_id,
				client_secret=client_secret,
				audience=auth.audience,
				base_url=auth.base_url,
				required_scopes=scopes or ["openid", "email"],
				client_storage=storage,
				jwt_signing_key=signing_key,
			)


def _oauth_storage(settings: Settings, signing_key: bytes) -> AsyncKeyValue:
	"""
	Where the OAuth proxy keeps client registrations, in-flight sign-ins and upstream tokens.

	In MongoDB, one ``mcp-*`` collection per kind with TTL indexes for expiry, encrypted because the
	upstream tokens are credentials for people's accounts at the provider.
	"""
	if settings.mcp.auth.storage == "memory":
		return MemoryStore()
	mongo = settings.mongo
	client: AsyncMongoClient = AsyncMongoClient(
		mongo.url.get_secret_value(),
		appname=settings.name,
		serverSelectionTimeoutMS=mongo.server_selection_timeout_ms,
	)
	with warnings.catch_warnings():
		# py-key-value may change this store's format in a future (locked) version. Entries it can't
		# read count as missing, so the worst case after upgrading is that people sign in again.
		warnings.filterwarnings("ignore", message="A configured store is unstable", category=UserWarning)
		store = MongoDBStore(client=client, db_name=mongo.database)
	# Undecryptable entries (after changing jwt_signing_key) read as missing too.
	key = derive_jwt_key(high_entropy_material=signing_key.decode(), salt="fastmcp-storage-encryption-key")
	return FernetEncryptionWrapper(store, fernet=Fernet(key), raise_on_decryption_error=False)


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
