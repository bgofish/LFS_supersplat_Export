from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from supersplat.api import ApiClient
from supersplat.errors import ApiError, PartUploadError, ProtocolError


class _ServerState:
    def __init__(self) -> None:
        self.responses: dict[
            tuple[str, str], tuple[int, dict[str, str], bytes]
        ] = {}
        self.requests: list[tuple[str, str, str | None]] = []

    def respond(
        self,
        method: str,
        path: str,
        status: int,
        value: object = None,
        *,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> None:
        payload = raw if raw is not None else json.dumps(value).encode("utf-8")
        self.responses[(method, path)] = (status, headers or {}, payload)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def _handle(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        if size:
            self.rfile.read(size)
        self.state.requests.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        status, headers, payload = self.state.responses.get(
            (self.command, self.path), (200, {}, b"{}")
        )
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server:
    def __init__(self) -> None:
        self.state = _ServerState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class ApiClientTests(unittest.TestCase):
    def test_api_redirect_is_rejected_before_bearer_can_be_forwarded(self) -> None:
        with _Server() as redirect, _Server() as target:
            redirect.state.respond(
                "GET",
                "/v1/me",
                302,
                headers={"Location": f"{target.base_url}/capture"},
            )

            with self.assertRaises(ApiError) as raised:
                ApiClient("secret", redirect.base_url, allow_http=True).get_me()

            self.assertEqual(raised.exception.status, 302)
            self.assertEqual(
                redirect.state.requests, [("GET", "/v1/me", "Bearer secret")]
            )
            self.assertEqual(target.state.requests, [])

    def test_opaque_ids_are_encoded_as_single_path_segments(self) -> None:
        with _Server() as server:
            expires_at = "2027-01-01T00:00:00Z"
            server.state.respond(
                "GET",
                "/v1/splats/splat%2Fone",
                200,
                {
                    "id": "splat/one",
                    "title": "Test splat",
                    "description": "",
                    "status": "processing",
                    "visibility": "unlisted",
                    "format": None,
                    "size": 0,
                    "viewerUrl": "https://superspl.at/scene/test",
                    "createdAt": "2026-07-15T12:00:00Z",
                    "updatedAt": "2026-07-15T12:00:00Z",
                    "completedAt": None,
                },
            )
            server.state.respond(
                "GET",
                "/v1/splats/uploads/upload%2Fone",
                200,
                {
                    "id": "upload/one",
                    "status": "uploading",
                    "contentLength": 4,
                    "sourceFormat": "ply",
                    "title": "Test splat",
                    "description": "",
                    "partSize": 4,
                    "uploadedParts": [],
                    "expiresAt": expires_at,
                },
            )
            server.state.respond(
                "POST",
                "/v1/splats/uploads/upload%2Fone/part-upload-urls",
                200,
                {
                    "uploadId": "upload/one",
                    "partSize": 4,
                    "urls": [
                        {
                            "partNumber": 1,
                            "url": "https://storage.example/part-1",
                            "expiresAt": expires_at,
                        }
                    ],
                },
            )
            server.state.respond(
                "POST",
                "/v1/splats/uploads/upload%2Fone/complete",
                200,
                {
                    "uploadId": "upload/one",
                    "status": "completed",
                    "splatId": "splat/one",
                    "editUrl": "https://superspl.at/scene/test/edit",
                },
            )
            client = ApiClient("secret", server.base_url, allow_http=True)
            client.get_splat("splat/one")
            client.get_upload("upload/one")
            client.create_part_urls("upload/one", [1])
            client.complete_upload("upload/one", [{"partNumber": 1, "etag": "e"}])

            self.assertEqual(
                [path for _method, path, _auth in server.state.requests],
                [
                    "/v1/splats/splat%2Fone",
                    "/v1/splats/uploads/upload%2Fone",
                    "/v1/splats/uploads/upload%2Fone/part-upload-urls",
                    "/v1/splats/uploads/upload%2Fone/complete",
                ],
            )

    def test_api_error_exposes_delta_seconds_retry_after(self) -> None:
        with _Server() as server:
            server.state.respond(
                "GET",
                "/v1/me",
                429,
                {"error": "slow down"},
                headers={"Retry-After": "7"},
            )

            with self.assertRaises(ApiError) as raised:
                ApiClient("secret", server.base_url, allow_http=True).get_me()

            self.assertEqual(raised.exception.retry_after, 7.0)
            self.assertTrue(raised.exception.transient)

    def test_part_error_exposes_http_date_retry_after(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _Server() as server:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
            server.state.respond(
                "PUT",
                "/part",
                503,
                {"error": "unavailable"},
                headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
            )
            path = Path(directory) / "part.bin"
            path.write_bytes(b"data")

            with self.assertRaises(PartUploadError) as raised:
                ApiClient("secret", server.base_url, allow_http=True).put_part(
                    f"{server.base_url}/part",
                    path,
                    0,
                    4,
                    threading.Event(),
                    lambda _size: None,
                )

            self.assertGreater(raised.exception.retry_after or 0, 25)
            self.assertLessEqual(raised.exception.retry_after or 31, 30)
            self.assertEqual(server.state.requests[0][2], None)

    def test_invalid_retry_after_is_ignored_and_409_is_not_transient(self) -> None:
        with _Server() as server:
            server.state.respond(
                "GET",
                "/v1/me",
                409,
                {"error": "invalid state"},
                headers={"Retry-After": "eventually"},
            )

            with self.assertRaises(ApiError) as raised:
                ApiClient("secret", server.base_url, allow_http=True).get_me()

            self.assertIsNone(raised.exception.retry_after)
            self.assertFalse(raised.exception.transient)

    def test_non_object_and_invalid_json_responses_are_protocol_errors(self) -> None:
        with _Server() as server:
            server.state.respond("GET", "/v1/me", 200, [])
            client = ApiClient("secret", server.base_url, allow_http=True)
            with self.assertRaises(ProtocolError):
                client.get_me()

            server.state.respond("GET", "/v1/me", 200, raw=b"not-json")
            with self.assertRaises(ProtocolError):
                client.get_me()


if __name__ == "__main__":
    unittest.main()
