from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from supersplat.errors import ApiError, ProtocolError
from supersplat.models import JobStatus, UploadJob, UploadedPart, utc_now
from supersplat.package_info import PLUGIN_VERSION, UPLOAD_CLIENT_ID, USER_AGENT
from supersplat.uploader import UploadCallbacks, UploadEngine, merge_parts, part_size_for


_TIMESTAMP = "2099-01-01T00:00:00Z"


class _ServerState:
    def __init__(self) -> None:
        self.part_size = 5
        self.url_requests: list[list[int]] = []
        self.puts: dict[int, bytes] = {}
        self.put_attempts: dict[int, int] = {}
        self.put_authorization: list[str | None] = []
        self.completed: dict | None = None
        self.fail_part_two_once = False
        self.resume_parts: list[dict] = []
        self.session_status = "uploading"
        self.session_content_length = 12
        self.session_source_format = "ply"
        self.session_overrides: dict = {}
        self.include_terminal_result = False
        self.part_url_overrides: dict = {}
        self.complete_overrides: dict = {}
        self.splat_overrides: dict = {}
        self.account_overrides: dict = {}
        self.create_count = 0
        self.create_body: dict | None = None
        self.api_user_agents: list[str | None] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args) -> None:
        pass

    def _json(self, status: int, value: dict) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size)) if size else {}

    def _session(
        self,
        *,
        status: str,
        content_length: int,
        source_format: str,
        uploaded_parts: list[dict],
    ) -> dict:
        value = {
            "id": "up-1",
            "status": status,
            "contentLength": content_length,
            "sourceFormat": source_format,
            "title": "Test scene",
            "description": "",
            "partSize": self.state.part_size,
            "uploadedParts": uploaded_parts,
            "expiresAt": "2099-01-02T00:00:00Z",
        }
        if self.state.include_terminal_result:
            value.update({
                "splatId": "splat-1",
                "editUrl": "https://editor.test/splat-1",
            })
        value.update(self.state.session_overrides)
        return value

    def _splat(self) -> dict:
        value = {
            "id": "splat-1",
            "title": "Test scene",
            "description": "",
            "status": "processing",
            "visibility": "unlisted",
            "format": None,
            "size": 0,
            "viewerUrl": "https://viewer.test/splat-1",
            "createdAt": _TIMESTAMP,
            "updatedAt": _TIMESTAMP,
            "completedAt": None,
        }
        value.update(self.state.splat_overrides)
        return value

    def do_GET(self) -> None:
        self.assert_bearer()
        if self.path == "/v1/me":
            value = {"id": "acct-1", "username": "Test User"}
            value.update(self.state.account_overrides)
            self._json(200, value)
        elif self.path == "/v1/splats/uploads/up-1":
            self._json(200, self._session(
                status=self.state.session_status,
                content_length=self.state.session_content_length,
                source_format=self.state.session_source_format,
                uploaded_parts=self.state.resume_parts,
            ))
        elif self.path == "/v1/splats/splat-1":
            self._json(200, self._splat())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.assert_bearer()
        body = self._body()
        if self.path == "/v1/splats/uploads":
            self.state.create_count += 1
            self.state.create_body = body
            self._json(200, self._session(
                status="created",
                content_length=body["contentLength"],
                source_format=body["sourceFormat"],
                uploaded_parts=[],
            ))
        elif self.path == "/v1/splats/uploads/up-1/part-upload-urls":
            parts = body["parts"]
            self.state.url_requests.append(parts)
            generation = len(self.state.url_requests)
            value = {
                "uploadId": "up-1",
                "partSize": self.state.part_size,
                "urls": [{
                    "partNumber": number,
                    "url": (
                        f"http://127.0.0.1:{self.server.server_port}"
                        f"/part/{generation}/{number}"
                    ),  # type: ignore[attr-defined]
                    "expiresAt": "2099-01-01T00:20:00Z",
                } for number in parts],
            }
            value.update(self.state.part_url_overrides)
            self._json(200, value)
        elif self.path == "/v1/splats/uploads/up-1/complete":
            self.state.completed = body
            value = {
                "uploadId": "up-1",
                "splatId": "splat-1",
                "status": "completed",
                "editUrl": "https://editor.test/splat-1",
            }
            value.update(self.state.complete_overrides)
            self._json(200, value)
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        bits = self.path.split("/")
        number = int(bits[-1])
        size = int(self.headers["Content-Length"])
        data = self.rfile.read(size)
        self.state.put_authorization.append(self.headers.get("Authorization"))
        attempts = self.state.put_attempts.get(number, 0) + 1
        self.state.put_attempts[number] = attempts
        if number == 2 and self.state.fail_part_two_once and attempts == 1:
            self._json(503, {"error": "try again"})
            return
        self.state.puts[number] = data
        self.send_response(200)
        self.send_header("ETag", f'"etag-{number}-{bits[-2]}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def assert_bearer(self) -> None:
        self.state.api_user_agents.append(self.headers.get("User-Agent"))
        if self.headers.get("Authorization") != "Bearer secret":
            raise AssertionError("missing API bearer token")


class _FakeServer:
    def __init__(self) -> None:
        self.state = _ServerState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


def _job(path: Path, base_url: str, *, upload_id: str = "") -> UploadJob:
    now = utc_now()
    return UploadJob(
        id="job-1", created_at=now, updated_at=now, status=JobStatus.UPLOADING,
        file_path=str(path), source_format="ply", title="Test scene", description="",
        node_names=["Splat"], sh_degree=3, idempotency_key="idem-1",
        base_url=base_url, file_size=path.stat().st_size, upload_id=upload_id,
    )


class UploadEngineTests(unittest.TestCase):
    def test_uploads_parts_finalizes_and_never_leaks_bearer_to_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.ply"
            path.write_bytes(b"abcdefghijkl")
            checkpoints: list[list[UploadedPart]] = []
            progress = []
            outcome = UploadEngine(concurrency=3, retries=0, allow_http=True).upload(
                _job(path, fake.base_url), "secret", threading.Event(),
                UploadCallbacks(
                    on_progress=progress.append,
                    on_checkpoint=lambda _upload_id, parts: checkpoints.append(list(parts)),
                ),
            )
            self.assertEqual(fake.state.puts, {1: b"abcde", 2: b"fghij", 3: b"kl"})
            self.assertTrue(all(value is None for value in fake.state.put_authorization))
            self.assertEqual(fake.state.create_body, {
                "title": "Test scene",
                "sourceFormat": "ply",
                "contentLength": 12,
                "softwareTools": ["lichtfeld-studio"],
                "uploadClient": {
                    "id": UPLOAD_CLIENT_ID,
                    "version": PLUGIN_VERSION,
                },
            })
            self.assertTrue(fake.state.api_user_agents)
            self.assertTrue(
                all(value == USER_AGENT for value in fake.state.api_user_agents)
            )
            self.assertEqual(
                [part["partNumber"] for part in fake.state.completed["parts"]],  # type: ignore[index]
                [1, 2, 3],
            )
            self.assertEqual(outcome.splat_id, "splat-1")
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.edit_url, "https://editor.test/splat-1")
            self.assertEqual(outcome.viewer_url, "https://viewer.test/splat-1")
            self.assertEqual(progress[-1].uploaded_bytes, 12)
            self.assertGreaterEqual(len(checkpoints), 4)

    def test_resume_uses_server_parts_as_authoritative_and_refreshes_retry_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.sog"
            path.write_bytes(b"abcdefghijkl")
            fake.state.resume_parts = [
                {"partNumber": 1, "etag": '"server-etag"', "size": 5}
            ]
            fake.state.fail_part_two_once = True
            job = _job(path, fake.base_url, upload_id="up-1")
            job.uploaded_parts = [
                UploadedPart(1, '"stale-local"', 5),
                UploadedPart(3, '"local-only"', 2),
            ]
            checkpoints: list[list[UploadedPart]] = []
            with patch.object(UploadEngine, "_wait_for_retry", return_value=None):
                UploadEngine(concurrency=2, retries=1, allow_http=True).upload(
                    job,
                    "secret",
                    threading.Event(),
                    UploadCallbacks(
                        on_checkpoint=lambda _upload_id, parts: checkpoints.append(
                            list(parts)
                        )
                    ),
                )
            self.assertEqual(fake.state.create_count, 0)
            self.assertNotIn(1, fake.state.puts)
            self.assertEqual(fake.state.puts[3], b"kl")
            self.assertEqual(fake.state.put_attempts[2], 2)
            self.assertIn([2], fake.state.url_requests[1:])
            self.assertEqual(
                [part.part_number for part in checkpoints[0]],
                [1],
                "the first checkpoint must discard local-only parts",
            )
            final = fake.state.completed["parts"]  # type: ignore[index]
            self.assertEqual(final[0]["etag"], '"server-etag"')

    def test_terminal_upload_session_recovers_existing_result(self) -> None:
        for status in ("processing", "completed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
                path = Path(directory) / "scene.ply"
                path.write_bytes(b"abcdefghijkl")
                fake.state.session_status = status
                fake.state.include_terminal_result = True
                outcome = UploadEngine(retries=0, allow_http=True).upload(
                    _job(path, fake.base_url, upload_id="up-1"),
                    "secret",
                    threading.Event(),
                )
                self.assertEqual(outcome.upload_id, "up-1")
                self.assertEqual(outcome.splat_id, "splat-1")
                self.assertEqual(outcome.status, status)
                self.assertEqual(outcome.edit_url, "https://editor.test/splat-1")
                self.assertEqual(fake.state.puts, {})
                self.assertIsNone(fake.state.completed)

    def test_terminal_upload_session_requires_result_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.ply"
            path.write_bytes(b"abcdefghijkl")
            fake.state.session_status = "processing"
            with self.assertRaisesRegex(ProtocolError, "splatId"):
                UploadEngine(retries=0, allow_http=True).upload(
                    _job(path, fake.base_url, upload_id="up-1"),
                    "secret",
                    threading.Event(),
                )

    def test_resumed_session_must_match_staged_job_and_use_valid_types(self) -> None:
        cases = {
            "upload id": {"id": "up-other"},
            "content length": {"contentLength": 13},
            "source format": {"sourceFormat": "sog"},
            "part size type": {"partSize": "5"},
            "status type": {"status": 7},
            "uploaded parts type": {"uploadedParts": {}},
            "uploaded part number": {
                "uploadedParts": [{"partNumber": "1", "etag": '"etag"'}]
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
                path = Path(directory) / "scene.ply"
                path.write_bytes(b"abcdefghijkl")
                fake.state.session_overrides = overrides
                with self.assertRaises(ProtocolError):
                    UploadEngine(retries=0, allow_http=True).upload(
                        _job(path, fake.base_url, upload_id="up-1"),
                        "secret",
                        threading.Event(),
                    )

    def test_signed_url_response_must_match_session_and_use_valid_types(self) -> None:
        cases = {
            "upload id": {"uploadId": "up-other"},
            "part size": {"partSize": 6},
            "part number type": {
                "urls": [{
                    "partNumber": "1",
                    "url": "http://storage.test/part/1",
                    "expiresAt": "2099-01-01T00:20:00Z",
                }]
            },
            "url type": {
                "urls": [{
                    "partNumber": 1,
                    "url": None,
                    "expiresAt": "2099-01-01T00:20:00Z",
                }]
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
                path = Path(directory) / "scene.ply"
                path.write_bytes(b"abcdefghijkl")
                fake.state.part_url_overrides = overrides
                with self.assertRaises(ProtocolError):
                    UploadEngine(retries=0, allow_http=True).upload(
                        _job(path, fake.base_url), "secret", threading.Event()
                    )

    def test_completion_response_must_match_session_and_be_completed(self) -> None:
        cases = {
            "upload id": {"uploadId": "up-other"},
            "status": {"status": "processing"},
            "splat id type": {"splatId": None},
            "edit url type": {"editUrl": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
                path = Path(directory) / "scene.ply"
                path.write_bytes(b"abcdefghijkl")
                fake.state.complete_overrides = overrides
                with self.assertRaises(ProtocolError):
                    UploadEngine(retries=0, allow_http=True).upload(
                        _job(path, fake.base_url), "secret", threading.Event()
                    )

    def test_used_account_and_splat_fields_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.ply"
            path.write_bytes(b"abcdefghijkl")
            fake.state.account_overrides = {"id": None}
            accounts: list[dict] = []
            with self.assertRaisesRegex(ProtocolError, "account"):
                UploadEngine(retries=0, allow_http=True).upload(
                    _job(path, fake.base_url),
                    "secret",
                    threading.Event(),
                    UploadCallbacks(on_account=accounts.append),
                )
            self.assertEqual(accounts, [])

        cases = {
            "splat id type": {"id": None},
            "splat id mismatch": {"id": "splat-other"},
            "viewer URL type": {"viewerUrl": None},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
                path = Path(directory) / "scene.ply"
                path.write_bytes(b"abcdefghijkl")
                fake.state.splat_overrides = overrides
                with self.assertRaises(ProtocolError):
                    UploadEngine(retries=0, allow_http=True).upload(
                        _job(path, fake.base_url), "secret", threading.Event()
                    )

    def test_signed_urls_are_requested_in_concurrency_sized_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.ply"
            path.write_bytes(b"abcdefghijkl")
            fake.state.part_size = 1
            UploadEngine(concurrency=3, retries=0, allow_http=True).upload(
                _job(path, fake.base_url), "secret", threading.Event()
            )
            self.assertEqual(
                fake.state.url_requests,
                [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]],
            )

    def test_rejects_session_that_would_require_more_than_ten_thousand_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _FakeServer() as fake:
            path = Path(directory) / "scene.ply"
            path.write_bytes(b"x" * 10_001)
            fake.state.part_size = 1
            with self.assertRaisesRegex(ProtocolError, "at most 10000"):
                UploadEngine(retries=0, allow_http=True).upload(
                    _job(path, fake.base_url), "secret", threading.Event()
                )
            self.assertEqual(fake.state.url_requests, [])

    def test_retry_passes_retry_after_to_cancellable_backoff(self) -> None:
        engine = UploadEngine(retries=1)
        error = ApiError("slow down", status=429, retry_after=2.5)
        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error
            return "ok"

        cancel_event = threading.Event()
        with patch.object(engine, "_wait_for_retry") as wait:
            self.assertEqual(engine._retry(operation, cancel_event), "ok")
        wait.assert_called_once_with(0, cancel_event, 2.5)

    def test_part_helpers_cover_last_part_and_server_is_authoritative(self) -> None:
        self.assertEqual(part_size_for(12, 5, 3), 2)
        merged = merge_parts(
            [UploadedPart(1, "local", 5), UploadedPart(2, "local-only", 5)],
            [UploadedPart(1, "server")],
            12,
            5,
            3,
        )
        self.assertEqual(set(merged), {1})
        self.assertEqual(merged[1].etag, "server")
        self.assertEqual(merged[1].size, 5)


if __name__ == "__main__":
    unittest.main()
