from fastapi import HTTPException, Query
from starlette.responses import Response

from app.core.files import FILES_PREFIX, FileStore
from app.core.settings import Settings
from app.routes.base import Routes, get


class FileRoutes(Routes):
	"""
	Serves files created by tools through signed, expiring links.

	The signature is the credential: browsers following a link won't carry the MCP
	bearer token, so this route sits outside MCP auth. Only the path is access-logged,
	never the signature.

	:param settings: Application settings.
	:type settings: Settings
	:param files: The same store the file-creating toolsets write to.
	:type files: FileStore
	"""

	prefix = FILES_PREFIX
	tags = ["files"]

	def __init__(self, settings: Settings, files: FileStore) -> None:
		super().__init__(settings)
		self.files = files

	@get("/{file_id}", summary="Download a generated file")
	async def download(
		self,
		file_id: str,
		expires: int = Query(description="Link expiry, Unix seconds"),
		sig: str = Query(description="Link signature"),
	) -> Response:
		if not self.files.verify(file_id, expires, sig):
			raise HTTPException(status_code=403, detail="Invalid or expired download link")
		response = await self.files.download_response(file_id)
		if response is None:
			raise HTTPException(status_code=404, detail="File not found")
		return response
