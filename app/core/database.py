from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from beanie import Document, init_beanie
from pymongo import AsyncMongoClient

from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger("app.database")


class Database:
	"""
	MongoDB connection with Beanie, opened and closed with the app.

	Pass :meth:`lifespan` to ``ServerBuilder.with_lifespan`` so every worker process opens
	its own client at startup, and :meth:`ping` to the readiness probe.

	:param settings: Application settings; ``settings.mongo`` configures the connection.
	:type settings: Settings
	:param models: Beanie document classes to initialise (collections and indexes).
	:type models: Sequence[type[Document]]
	"""

	def __init__(self, settings: Settings, models: Sequence[type[Document]]) -> None:
		self.settings = settings
		self.models = list(models)
		self._client: AsyncMongoClient | None = None

	@asynccontextmanager
	async def lifespan(self, _app: Any) -> AsyncIterator[None]:
		"""
		Asynchronous context manager for handling the lifespan of a MongoDB client connection
		and initializing Beanie models.

		This function manages the connection lifecycle for MongoDB using an `AsyncMongoClient`.
		It configures the client according to the provided settings, initializes Beanie models
		with the specified database, and ensures the client is properly closed once the context
		has ended.

		:param _app: Application instance used during the context
		:type _app: Any
		:return: Asynchronous iterator that manages the connection and wrapping context
		:rtype: AsyncIterator[None]
		"""
		config = self.settings.mongo
		client: AsyncMongoClient = AsyncMongoClient(
			config.url.get_secret_value(),
			appname=self.settings.name,
			serverSelectionTimeoutMS=config.server_selection_timeout_ms,
			tz_aware=True,
		)
		self._client = client
		try:
			await init_beanie(database=client[config.database], document_models=self.models)
			log.info("mongo.connected", database=config.database, models=[m.__name__ for m in self.models])
			yield
		finally:
			self._client = None
			await client.close()
			log.info("mongo.closed")

	async def ping(self) -> None:
		"""
		Sends a ping command to the database to verify the connection is active.

		This method checks if the database client is connected and sends a "ping"
		command to the database.

		:raises RuntimeError: If the database client is not connected.
		:return: None
		:rtype: None
		"""
		if self._client is None:
			raise RuntimeError("database is not connected")
		await self._client.admin.command("ping")
