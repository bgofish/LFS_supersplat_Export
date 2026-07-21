"""Small, dependency-free client for the SuperSplat upload API."""

from __future__ import annotations

import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator

from .errors import ApiError, ConfigurationError, PartUploadError, ProtocolError, UploadCancelled
from .package_info import USER_AGENT

DEFAULT_BASE_URL = "https://playcanvas.com/api/supersplat"


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Turn API redirects into HTTP errors before credentials can be forwarded."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class _RangedBody:
    def __init__(
        self,
        path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.path = path
        self.start = start
        self.size = size
        self.cancel_event = cancel_event
        self.on_bytes = on_bytes
        self.chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        remaining = self.size
        with self.path.open("rb") as stream:
            stream.seek(self.start)
            while remaining:
                if self.cancel_event.is_set():
                    raise UploadCancelled("Upload cancelled.")
                data = stream.read(min(self.chunk_size, remaining))
                if not data:
                    raise ProtocolError(
                        f"Export file ended before the expected {self.size} byte range."
                    )
                remaining -= len(data)
                self.on_bytes(len(data))
                yield data


class ApiClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        allow_http: bool = False,
    ) -> None:
        if not api_key.strip():
            raise ConfigurationError("Enter a PlayCanvas API key first.")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.allow_http = allow_http
        self._validate_url(self.base_url, signed=False)
        self._api_opener = urllib.request.build_opener(_RejectRedirects())

    def get_me(self) -> dict[str, Any]:
        return self._object(self._request_json("GET", "/v1/me"), "account")

    def get_splat(self, splat_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(splat_id, safe="")
        return self._object(
            self._request_json("GET", f"/v1/splats/{encoded_id}"),
            "splat",
        )

    def create_upload(
        self, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._object(
            self._request_json("POST", "/v1/splats/uploads", body, headers),
            "upload session",
        )

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(upload_id, safe="")
        return self._object(
            self._request_json("GET", f"/v1/splats/uploads/{encoded_id}"),
            "upload session",
        )

    def create_part_urls(
        self, upload_id: str, part_numbers: list[int]
    ) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(upload_id, safe="")
        return self._object(
            self._request_json(
                "POST",
                f"/v1/splats/uploads/{encoded_id}/part-upload-urls",
                {"parts": part_numbers},
            ),
            "signed URL response",
        )

    def complete_upload(
        self, upload_id: str, parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(upload_id, safe="")
        return self._object(
            self._request_json(
                "POST",
                f"/v1/splats/uploads/{encoded_id}/complete",
                {"parts": parts},
            ),
            "completed upload",
        )

    def put_part(
        self,
        url: str,
        file_path: Path,
        start: int,
        size: int,
        cancel_event: Event,
        on_bytes: Callable[[int], None],
    ) -> str:
        self._validate_url(url, signed=True)
        body = _RangedBody(file_path, start, size, cancel_event, on_bytes)
        request = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={"Content-Length": str(size), "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                etag = response.headers.get("ETag")
                if not etag:
                    raise PartUploadError(
                        "The storage service accepted a part but returned no ETag.",
                        status=response.status,
                        url=url,
                    )
                return etag
        except UploadCancelled:
            raise
        except urllib.error.HTTPError as error:
            payload = _read_error(error)
            raise PartUploadError(
                _error_message(payload, error.code, error.reason),
                status=error.code,
                body=payload,
                url=url,
                retry_after=_retry_after(error.headers),
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PartUploadError(
                f"Part upload could not reach storage: {getattr(error, 'reason', error)}",
                url=url,
            ) from error

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._api_opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.status == 204:
                    return None
        except urllib.error.HTTPError as error:
            payload = _read_error(error)
            raise ApiError(
                _error_message(payload, error.code, error.reason),
                status=error.code,
                body=payload,
                url=url,
                retry_after=_retry_after(error.headers),
            ) from error
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as error:
            raise ApiError(
                f"Could not reach SuperSplat: {getattr(error, 'reason', error)}", url=url
            ) from error

        if not raw:
            raise ProtocolError(f"SuperSplat returned an empty response for {method} {path}.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(
                f"SuperSplat returned invalid JSON for {method} {path}."
            ) from error

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProtocolError(f"SuperSplat returned an invalid {label} response.")
        return value

    def _validate_url(self, value: str, *, signed: bool) -> None:
        parsed = urllib.parse.urlparse(value)
        if not parsed.netloc or parsed.scheme not in ({"https", "http"} if self.allow_http else {"https"}):
            label = "signed upload URL" if signed else "API base URL"
            raise ConfigurationError(f"The {label} must be a valid HTTPS URL.")


def _read_error(error: urllib.error.HTTPError) -> Any:
    try:
        raw = error.read()
        return json.loads(raw.decode("utf-8")) if raw else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _error_message(body: Any, status: int, reason: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return f"{status} {reason}".strip()


def _retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isdigit():
        try:
            delay = float(value)
        except (ValueError, OverflowError):
            return None
        return delay if math.isfinite(delay) else None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
