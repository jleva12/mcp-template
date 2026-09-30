from pydantic import BaseModel

from app.core.logging import get_logger
from app.routes.base import Routes, get

log = get_logger(__name__)


class Message(BaseModel):
	message: str


class HelloRoutes(Routes):
	tags = ["hello"]

	@get("/")
	async def root(self) -> Message:
		return Message(message="Hello World")

	@get("/hello/{name}")
	async def say_hello(self, name: str) -> Message:
		log.info("hello.greeted", name=name)
		return Message(message=f"Hello {name}")
