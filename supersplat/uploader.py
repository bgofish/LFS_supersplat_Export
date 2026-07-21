"""Resumable multipart upload orchestration."""

from __future__ import annotations

import math
import random
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .api import ApiClient, DEFAULT_BASE_URL
from .errors import ApiError, ConfigurationError, ProtocolError, UploadCancelled
from .models import ProgressSnapshot, UploadJob, UploadOutcome, UploadedPart
from .package_info import PLUGIN_VERSION, SOFTWARE_TOOLS, UPLOAD_CLIENT_ID
from .progress import ProgressTracker

MAX_SIGNED_URLS_PER_REQUEST = 100
MAX_MULTIPART_PARTS = 10_000
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024


def _ignore(*_args: Any) -> None:
    pass


@dataclass
class UploadCallbacks:
    on_stage: Callable[[str], None] = _ignore
    on_account: Callable[[dict[str, Any]], None] = _ignore
    on_progress: Callable[[ProgressSnapshot], None] = _ignore
    on_checkpoint: Callable[[str, list[UploadedPart]], None] = _ignore


@dataclass(frozen=True)
class _ValidatedUploadSession:
    upload_id: str
    status: str
    part_size: int
    uploaded_parts: list[UploadedPart]


class UploadEngine:
    def __init__(
        self,
        *,
        concurrency: int = 4,
        retries: int = 3,
        timeout: float = 30.0,
        allow_http: bool = False,
    ) -> None:
        self.concurrency = max(1, min(int(concurrency), 8))
        self.retries = max(0, int(retries))
        self.timeout = timeout
        self.allow_http = allow_http

    def upload(
        self,
        job: UploadJob,
        api_key: str,
        cancel_event: threading.Event,
        callbacks: UploadCallbacks | None = None,
    ) -> UploadOutcome:
        callbacks = callbacks or UploadCallbacks()
        path = Path(job.file_path)
        file_size = self._validate_file(path, job.file_size)
        self._check_cancel(cancel_event)
        client = ApiClient(
            api_key,
            job.base_url or DEFAULT_BASE_URL,
            timeout=self.timeout,
            allow_http=self.allow_http,
        )

        callbacks.on_stage("Authenticating with SuperSplat...")
        account = self._retry(client.get_me, cancel_event)
        account_id = _required_string(account, "id", "account")
        if job.account_id and job.account_id != account_id:
            raise ConfigurationError(
                "This upload was started with a different SuperSplat account."
            )
        callbacks.on_account(account)

        session, expected_upload_id = self._get_or_create_session(
            client, job, file_size, cancel_event, callbacks
        )
        validated = _validate_upload_session(
            session,
            expected_upload_id=expected_upload_id,
            expected_content_length=file_size,
            expected_source_format=job.source_format,
        )
        upload_id = validated.upload_id
        if validated.status in {"processing", "completed"}:
            return self._outcome_from_terminal_session(
                client, session, validated, cancel_event
            )
        if validated.status not in {"created", "uploading"}:
            raise ProtocolError(
                "Upload session "
                f"{upload_id} cannot be resumed because it is {validated.status}."
            )

        part_size = validated.part_size
        total_parts = math.ceil(file_size / part_size)
        if total_parts < 1:
            raise ConfigurationError("The staged export is empty.")
        if total_parts > MAX_MULTIPART_PARTS:
            raise ProtocolError(
                "SuperSplat returned a part size that would require "
                f"{total_parts} parts; the upload API supports at most "
                f"{MAX_MULTIPART_PARTS}."
            )
        completed = merge_parts(
            job.uploaded_parts,
            validated.uploaded_parts,
            file_size,
            part_size,
            total_parts,
        )
        callbacks.on_checkpoint(
            upload_id, [completed[key] for key in sorted(completed)]
        )
        tracker = ProgressTracker(
            file_size,
            total_parts,
            {number: part.size for number, part in completed.items()},
        )
        callbacks.on_progress(tracker.snapshot())
        missing = [number for number in range(1, total_parts + 1) if number not in completed]
        callbacks.on_stage(
            "Finalizing previously uploaded parts..."
            if not missing
            else f"Uploading {len(missing)} of {total_parts} parts..."
        )

        checkpoint_lock = threading.Lock()
        url_lock = threading.Lock()
        signing_window = min(self.concurrency, MAX_SIGNED_URLS_PER_REQUEST)
        for batch in _chunks(missing, signing_window):
            self._check_cancel(cancel_event)
            signed = self._signed_urls(client, upload_id, batch, part_size, cancel_event)

            def upload_one(part_number: int) -> UploadedPart:
                current_url = signed[part_number]
                last_error: Exception | None = None
                for attempt in range(self.retries + 1):
                    self._check_cancel(cancel_event)
                    tracker.begin_attempt(part_number)
                    try:
                        size = part_size_for(file_size, part_size, part_number)
                        etag = client.put_part(
                            current_url,
                            path,
                            (part_number - 1) * part_size,
                            size,
                            cancel_event,
                            lambda count: callbacks.on_progress(
                                tracker.add_bytes(part_number, count)
                            ),
                        )
                        part = UploadedPart(part_number, etag, size)
                        callbacks.on_progress(tracker.commit(part_number, size))
                        with checkpoint_lock:
                            completed[part_number] = part
                            callbacks.on_checkpoint(
                                upload_id,
                                [completed[key] for key in sorted(completed)],
                            )
                        return part
                    except UploadCancelled:
                        tracker.reset_attempt(part_number)
                        raise
                    except ApiError as error:
                        last_error = error
                        callbacks.on_progress(tracker.reset_attempt(part_number))
                        if attempt >= self.retries or not error.transient:
                            raise
                        self._wait_for_retry(
                            attempt,
                            cancel_event,
                            getattr(error, "retry_after", None),
                        )
                        with url_lock:
                            current_url = self._signed_urls(
                                client, upload_id, [part_number], part_size, cancel_event
                            )[part_number]
                assert last_error is not None
                raise last_error

            with ThreadPoolExecutor(
                max_workers=min(self.concurrency, max(len(batch), 1)),
                thread_name_prefix="supersplat-part",
            ) as executor:
                futures: list[Future[UploadedPart]] = [
                    executor.submit(upload_one, number) for number in batch
                ]
                try:
                    for future in as_completed(futures):
                        future.result()
                except Exception:
                    cancel_event.set()
                    for future in futures:
                        future.cancel()
                    raise

        self._check_cancel(cancel_event)
        if len(completed) != total_parts:
            raise ProtocolError(
                f"Upload is incomplete: expected {total_parts} parts, got {len(completed)}."
            )
        callbacks.on_stage("Finalizing upload...")
        final_parts = [
            {"partNumber": number, "etag": completed[number].etag}
            for number in range(1, total_parts + 1)
        ]
        result = self._retry(
            lambda: client.complete_upload(upload_id, final_parts), cancel_event
        )
        return self._outcome_from_completion(
            client, result, upload_id, cancel_event
        )

    def _get_or_create_session(
        self,
        client: ApiClient,
        job: UploadJob,
        file_size: int,
        cancel_event: threading.Event,
        callbacks: UploadCallbacks,
    ) -> tuple[dict[str, Any], str | None]:
        if job.upload_id:
            callbacks.on_stage("Checking previous upload session...")
            try:
                return (
                    self._retry(
                        lambda: client.get_upload(job.upload_id), cancel_event
                    ),
                    job.upload_id,
                )
            except ApiError as error:
                if error.status != 404:
                    raise
        callbacks.on_stage("Creating upload session...")
        body: dict[str, Any] = {
            "contentLength": file_size,
            "softwareTools": list(SOFTWARE_TOOLS),
            "uploadClient": {
                "id": UPLOAD_CLIENT_ID,
                "version": PLUGIN_VERSION,
            },
            "title": job.title,
            "sourceFormat": job.source_format,
        }
        if job.description:
            body["description"] = job.description
        session = self._retry(
            lambda: client.create_upload(body, idempotency_key=job.idempotency_key),
            cancel_event,
        )
        return session, None

    def _signed_urls(
        self,
        client: ApiClient,
        upload_id: str,
        numbers: list[int],
        expected_part_size: int,
        cancel_event: threading.Event,
    ) -> dict[int, str]:
        response = self._retry(
            lambda: client.create_part_urls(upload_id, numbers), cancel_event
        )
        response_upload_id = _required_string(
            response, "uploadId", "signed URL response"
        )
        if response_upload_id != upload_id:
            raise ProtocolError(
                "SuperSplat returned signed URLs for a different upload session."
            )
        response_size = _required_positive_int(response, "partSize", "signed URL response")
        if response_size != expected_part_size:
            raise ProtocolError("SuperSplat changed the upload part size during the session.")
        values = response.get("urls")
        if not isinstance(values, list):
            raise ProtocolError("SuperSplat did not return signed part URLs.")
        result: dict[int, str] = {}
        requested = set(numbers)
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise ProtocolError(
                    "SuperSplat returned an invalid signed URL response: "
                    f"urls item {index + 1} must be an object."
                )
            number = _required_positive_int(
                item, "partNumber", f"signed URL item {index + 1}"
            )
            if number > MAX_MULTIPART_PARTS:
                raise ProtocolError(
                    "SuperSplat returned an invalid signed URL response: "
                    f"partNumber must not exceed {MAX_MULTIPART_PARTS}."
                )
            url = _required_string(item, "url", f"signed URL item {index + 1}")
            if number not in requested:
                raise ProtocolError(
                    f"SuperSplat returned an unrequested signed URL for part {number}."
                )
            if number in result:
                raise ProtocolError(
                    f"SuperSplat returned more than one signed URL for part {number}."
                )
            result[number] = url
        absent = sorted(requested - result.keys())
        if absent:
            raise ProtocolError(f"SuperSplat omitted signed URLs for parts {absent}.")
        return result

    def _outcome_from_terminal_session(
        self,
        client: ApiClient,
        value: dict[str, Any],
        session: _ValidatedUploadSession,
        cancel_event: threading.Event,
    ) -> UploadOutcome:
        return self._outcome_from_result(
            client,
            value,
            session.upload_id,
            session.status,
            "terminal upload session",
            cancel_event,
        )

    def _outcome_from_completion(
        self,
        client: ApiClient,
        value: dict[str, Any],
        upload_id: str,
        cancel_event: threading.Event,
    ) -> UploadOutcome:
        response_upload_id = _required_string(value, "uploadId", "completed upload")
        if response_upload_id != upload_id:
            raise ProtocolError(
                "SuperSplat completed a different upload session than requested."
            )
        status = _required_string(value, "status", "completed upload")
        if status != "completed":
            raise ProtocolError(
                "SuperSplat returned an invalid completed upload: "
                "status must be completed."
            )
        return self._outcome_from_result(
            client,
            value,
            upload_id,
            status,
            "completed upload",
            cancel_event,
        )

    def _outcome_from_result(
        self,
        client: ApiClient,
        value: dict[str, Any],
        upload_id: str,
        status: str,
        label: str,
        cancel_event: threading.Event,
    ) -> UploadOutcome:
        splat_id = _required_string(value, "splatId", label)
        edit_url = _required_string(value, "editUrl", label)
        splat = self._retry(lambda: client.get_splat(splat_id), cancel_event)
        returned_splat_id = _required_string(splat, "id", "splat")
        if returned_splat_id != splat_id:
            raise ProtocolError("SuperSplat returned a different splat than requested.")
        viewer_url = _required_string(splat, "viewerUrl", "splat")
        return UploadOutcome(
            upload_id=upload_id,
            splat_id=splat_id,
            status=status,
            edit_url=edit_url,
            viewer_url=viewer_url,
            splat=splat,
        )

    @staticmethod
    def _validate_file(path: Path, expected_size: int) -> int:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ConfigurationError(f"The staged export is unavailable: {path}") from error
        if size <= 0:
            raise ConfigurationError("The staged export is empty.")
        if size > MAX_FILE_SIZE:
            raise ConfigurationError("This plugin currently limits uploads to 10 GiB.")
        if expected_size and size != expected_size:
            raise ConfigurationError(
                "The staged export changed since the upload was created; export it again."
            )
        return size

    def _retry(self, operation: Callable[[], Any], cancel_event: threading.Event) -> Any:
        for attempt in range(self.retries + 1):
            self._check_cancel(cancel_event)
            try:
                return operation()
            except ApiError as error:
                if attempt >= self.retries or not error.transient:
                    raise
                self._wait_for_retry(
                    attempt,
                    cancel_event,
                    getattr(error, "retry_after", None),
                )
        raise AssertionError("unreachable")

    @staticmethod
    def _wait_for_retry(
        attempt: int,
        cancel_event: threading.Event,
        retry_after: float | None = None,
    ) -> None:
        delay = min(0.5 * (2**attempt) + random.random() * 0.25, 5.0)
        if retry_after is not None and retry_after >= 0:
            delay = max(delay, retry_after)
        if cancel_event.wait(delay):
            raise UploadCancelled("Upload cancelled.")

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise UploadCancelled("Upload cancelled.")


def part_size_for(file_size: int, part_size: int, part_number: int) -> int:
    start = (part_number - 1) * part_size
    return max(min(part_size, file_size - start), 0)


def merge_parts(
    local_parts: list[UploadedPart],
    server_parts: list[UploadedPart],
    file_size: int,
    part_size: int,
    total_parts: int,
) -> dict[int, UploadedPart]:
    result: dict[int, UploadedPart] = {}
    local_by_number = {
        part.part_number: part
        for part in local_parts
        if 1 <= part.part_number <= total_parts
    }
    for part in server_parts:
        if not 1 <= part.part_number <= total_parts:
            raise ProtocolError(
                "SuperSplat reported an uploaded part outside the staged file range."
            )
        if not part.etag:
            raise ProtocolError("SuperSplat reported an uploaded part without an ETag.")
        if part.part_number in result:
            raise ProtocolError(
                f"SuperSplat reported uploaded part {part.part_number} more than once."
            )
        local = local_by_number.get(part.part_number)
        local_size = local.size if local is not None and local.size > 0 else 0
        size = (
            part.size
            or local_size
            or part_size_for(file_size, part_size, part.part_number)
        )
        result[part.part_number] = UploadedPart(part.part_number, part.etag, size)
    return result


def _parts_from_response(value: Any, label: str) -> list[UploadedPart]:
    if not isinstance(value, list):
        raise ProtocolError(
            f"SuperSplat returned an invalid {label}: uploadedParts must be an array."
        )
    result: list[UploadedPart] = []
    seen: set[int] = set()
    for index, item in enumerate(value):
        item_label = f"{label} uploadedParts item {index + 1}"
        if not isinstance(item, dict):
            raise ProtocolError(
                f"SuperSplat returned an invalid {item_label}: expected an object."
            )
        number = _required_positive_int(item, "partNumber", item_label)
        if number > MAX_MULTIPART_PARTS:
            raise ProtocolError(
                f"SuperSplat returned an invalid {item_label}: partNumber must not "
                f"exceed {MAX_MULTIPART_PARTS}."
            )
        if number in seen:
            raise ProtocolError(
                f"SuperSplat returned uploaded part {number} more than once."
            )
        seen.add(number)
        etag = _required_string(item, "etag", item_label)
        size = 0
        if "size" in item:
            size = _required_nonnegative_int(item, "size", item_label)
        result.append(UploadedPart(number, etag, size))
    return result


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _required_string(value: dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProtocolError(f"SuperSplat returned an invalid {label}: missing {key}.")
    return result


def _required_positive_int(value: dict[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ProtocolError(
            f"SuperSplat returned an invalid {label}: "
            f"{key} must be a positive integer."
        )
    return result


def _required_nonnegative_int(value: dict[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ProtocolError(
            f"SuperSplat returned an invalid {label}: "
            f"{key} must be a non-negative integer."
        )
    return result


def _validate_upload_session(
    value: dict[str, Any],
    *,
    expected_upload_id: str | None,
    expected_content_length: int,
    expected_source_format: str,
) -> _ValidatedUploadSession:
    upload_id = _required_string(value, "id", "upload session")
    if expected_upload_id is not None and upload_id != expected_upload_id:
        raise ProtocolError(
            "SuperSplat returned a different upload session than requested."
        )

    status = _required_string(value, "status", "upload session")
    valid_statuses = {"created", "uploading", "processing", "completed", "canceled"}
    if status not in valid_statuses:
        raise ProtocolError(
            f"SuperSplat returned an invalid upload session status: {status}."
        )

    content_length = _required_nonnegative_int(
        value, "contentLength", "upload session"
    )
    if content_length != expected_content_length:
        raise ProtocolError(
            "The resumed SuperSplat upload has a different content length than "
            "the staged export."
        )

    source_format = _required_string(value, "sourceFormat", "upload session")
    if source_format != expected_source_format:
        raise ProtocolError(
            "The resumed SuperSplat upload has a different source format than "
            "the staged export."
        )

    part_size = _required_positive_int(value, "partSize", "upload session")
    uploaded_parts = _parts_from_response(value.get("uploadedParts"), "upload session")
    return _ValidatedUploadSession(upload_id, status, part_size, uploaded_parts)
