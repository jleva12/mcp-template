import time
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError

from app.core.files import FileStore, LinkSigner, S3FileStore, StoredFile, _safe_filename
from app.routes import FileRoutes
from app.tools import FileTools
from tests.conftest import MCP_HEADERS, make_settings


def _settings(tmp_path, **files):
	return make_settings(
		mcp={"stateless_http": True, "json_response": True},
		files={"signing_key": "test-key", "local_dir": str(tmp_path), **files},
	)


def _client_with_files(make_client, settings, store=None):
	store = store or FileStore.from_settings(settings)
	client = make_client(settings, routes=(FileRoutes(settings, store),), toolsets=(FileTools(settings, store),))
	return client, store


def _create_file(client, filename="notes.txt", content="hello file") -> dict:
	call = {
		"jsonrpc": "2.0",
		"id": 1,
		"method": "tools/call",
		"params": {"name": "create_text_file", "arguments": {"filename": filename, "content": content}},
	}
	[link] = client.post("/mcp", headers=MCP_HEADERS, json=call).json()["result"]["content"]
	return link


def test_signer_rejects_tampering_and_expiry():
	signer = LinkSigner(b"key")
	expires = int(time.time()) + 60
	sig = signer.sign("abc", expires)
	assert signer.verify("abc", expires, sig)
	assert not signer.verify("abd", expires, sig)
	assert not signer.verify("abc", expires + 1, sig)
	assert not LinkSigner(b"other-key").verify("abc", expires, sig)
	past = int(time.time()) - 1
	assert not signer.verify("abc", past, signer.sign("abc", past))


@pytest.mark.parametrize(
	("raw", "expected"),
	[("report.pdf", "report.pdf"), ("../../etc/passwd", "passwd"), ('a"b\n.txt', "ab.txt"), ("..", "download")],
)
def test_safe_filename(raw, expected):
	assert _safe_filename(raw) == expected


def test_tool_returns_link_that_downloads_the_file(make_client, tmp_path):
	client, _ = _client_with_files(make_client, _settings(tmp_path))
	link = _create_file(client)

	assert link["type"] == "resource_link"
	assert link["name"] == "notes.txt"
	assert link["mimeType"] == "text/plain"
	assert link["uri"].startswith("http://testserver/files/")

	response = client.get(link["uri"])
	assert response.status_code == 200
	assert response.text == "hello file"
	assert response.headers["content-disposition"] == 'attachment; filename="notes.txt"'
	assert response.headers["x-content-type-options"] == "nosniff"
	assert response.headers["cache-control"] == "private, no-store"


def test_download_rejects_bad_links(make_client, tmp_path):
	client, store = _client_with_files(make_client, _settings(tmp_path))
	url = urlsplit(_create_file(client)["uri"])
	file_id = url.path.rsplit("/", 1)[-1]
	query = {k: v[0] for k, v in parse_qs(url.query).items()}

	tampered = client.get(f"/files/{file_id}", params={**query, "sig": query["sig"][:-1] + "x"})
	assert tampered.status_code == 403
	expired_at = int(time.time()) - 1
	expired = client.get(
		f"/files/{file_id}", params={"expires": expired_at, "sig": store.signer.sign(file_id, expired_at)}
	)
	assert expired.status_code == 403
	missing_id = "0" * 32
	missing = store.download_url(StoredFile(id=missing_id, filename="x", content_type="text/plain", size=0))
	assert client.get(missing).status_code == 404
	assert client.get(f"/files/{file_id}").status_code == 422


def test_public_base_url_is_used_for_links(make_client, tmp_path):
	client, _ = _client_with_files(make_client, _settings(tmp_path, public_base_url="https://mcp.example.com/"))
	assert _create_file(client)["uri"].startswith("https://mcp.example.com/files/")


def test_signing_key_required_outside_development(tmp_path):
	settings = make_settings(environment="staging", files={"local_dir": str(tmp_path)})
	with pytest.raises(ValueError, match="signing_key"):
		FileStore.from_settings(settings)


class FakeS3:
	def __init__(self) -> None:
		self.objects: dict[str, dict] = {}

	def put_object(self, *, Bucket: str, Key: str, **kwargs) -> None:
		self.objects[Key] = {"Bucket": Bucket, **kwargs}

	def head_object(self, *, Bucket: str, Key: str) -> dict:
		if Key not in self.objects:
			raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
		return {}

	def generate_presigned_url(self, operation: str, *, Params: dict, ExpiresIn: int) -> str:
		return f"https://bucket.example.com/{Params['Key']}?ttl={ExpiresIn}"


def test_s3_backend_uploads_and_redirects(make_client, tmp_path):
	settings = _settings(tmp_path, backend="s3", s3_bucket="generated", s3_prefix="exports/")
	s3 = FakeS3()
	store = S3FileStore(settings, LinkSigner(b"test-key"), client=s3)
	client, _ = _client_with_files(make_client, settings, store)

	link = _create_file(client, filename="résumé.txt")
	[(key, stored)] = s3.objects.items()
	assert key.startswith("exports/")
	assert stored["Bucket"] == "generated"
	assert stored["ContentDisposition"] == "attachment; filename=\"rsum.txt\"; filename*=UTF-8''r%C3%A9sum%C3%A9.txt"

	response = client.get(link["uri"], follow_redirects=False)
	assert response.status_code == 302
	assert response.headers["location"] == f"https://bucket.example.com/{key}?ttl=60"

	s3.objects.clear()
	assert client.get(link["uri"], follow_redirects=False).status_code == 404
