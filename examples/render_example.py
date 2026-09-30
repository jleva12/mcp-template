"""Upload an example template to a running server, render it, and save the document.

Each example folder holds template.html, data.json and (optionally) schema.json.

	uv run --env-file .env python examples/render_example.py
	uv run --env-file .env python examples/render_example.py quarterly_report --format docx
	uv run --env-file .env python examples/render_example.py quarterly_report --correlation-id job-42
	uv run --env-file .env python examples/render_example.py quarterly_report --token <api-key>
	uv run python examples/render_example.py --base-url https://mcp.example.com --token <api-key>
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

EXAMPLES = Path(__file__).parent


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("example", nargs="?", default="quarterly_report", help="folder under examples/")
	parser.add_argument("--base-url", default="http://127.0.0.1:8000")
	parser.add_argument("--token", help="bearer token, if the server has auth enabled")
	parser.add_argument("--correlation-id", help="your own ID for this render, returned on the document")
	parser.add_argument("--format", choices=["pdf", "docx"], default="pdf", help="file type to render")
	args = parser.parse_args()

	folder = EXAMPLES / args.example
	schema = folder / "schema.json"
	headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

	with httpx.Client(base_url=args.base_url, headers=headers, timeout=120) as client:
		template = _json(
			client.post(
				"/templates",
				files={"file": ("template.html", (folder / "template.html").read_bytes(), "text/html")},
				data={"name": folder.name.replace("_", " ").title()}
				| ({"json_schema": schema.read_text()} if schema.exists() else {}),
			)
		)
		print(f"template  {template['template_id']}  (version {template['latest_version']})")

		body = {"template_data": json.loads((folder / "data.json").read_text()), "output_format": args.format}
		if args.correlation_id:
			body["correlation_id"] = args.correlation_id
		document = _json(client.post(f"/templates/{template['template_id']}/documents", json=body))
		pages = f"{document['page_count']} pages, " if document["page_count"] is not None else ""
		print(f"document  {document['document_id']}  ({pages}{document['size_bytes']:,} bytes)")
		if document["correlation_id"]:
			print(f"          correlation_id {document['correlation_id']}")

		file = _ok(client.get(f"/documents/{document['document_id']}/file", follow_redirects=True))
		output = folder / "output" / f"{folder.name}.{args.format}"
		output.parent.mkdir(exist_ok=True)
		output.write_bytes(file.content)
		print(f"saved     {output.relative_to(Path.cwd()) if output.is_relative_to(Path.cwd()) else output}")


def _ok(response: httpx.Response) -> httpx.Response:
	if response.is_error:
		sys.exit(
			f"{response.request.method} {response.request.url.path} failed ({response.status_code}): {response.text}"
		)
	return response


def _json(response: httpx.Response) -> dict[str, Any]:
	return _ok(response).json()


if __name__ == "__main__":
	main()
