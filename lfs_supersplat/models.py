"""Serializable models shared by the controller and upload engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


UPLOAD_JOB_SCHEMA_VERSION = 1
# Version 0 is the same flat layout written before manifests carried a version.
_LEGACY_UPLOAD_JOB_SCHEMA_VERSION = 0
_MISSING_SCHEMA_VERSION = object()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(str, Enum):
    EXPORTING = "exporting"
    AUTHENTICATING = "authenticating"
    CREATING_UPLOAD = "creating_upload"
    UPLOADING = "uploading"
    FINALIZING = "finalizing"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class UploadedPart:
    part_number: int
    etag: str
    size: int = 0

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "UploadedPart":
        return cls(
            part_number=int(value.get("partNumber", 0)),
            etag=str(value.get("etag", "")),
            size=int(value.get("size", 0) or 0),
        )

    def to_api(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "partNumber": self.part_number,
            "etag": self.etag,
        }
        if self.size > 0:
            result["size"] = self.size
        return result


@dataclass
class UploadJob:
    id: str
    created_at: str
    updated_at: str
    status: JobStatus
    file_path: str
    source_format: str
    title: str
    description: str
    node_names: list[str]
    sh_degree: int
    idempotency_key: str
    base_url: str
    file_size: int = 0
    account_id: str = ""
    account_name: str = ""
    upload_id: str = ""
    uploaded_parts: list[UploadedPart] = field(default_factory=list)
    splat_id: str = ""
    edit_url: str = ""
    viewer_url: str = ""
    error: str = ""

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["schema_version"] = UPLOAD_JOB_SCHEMA_VERSION
        result["status"] = self.status.value
        result["uploaded_parts"] = [part.to_api() for part in self.uploaded_parts]
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UploadJob":
        data = dict(value)
        raw_schema_version = data.pop("schema_version", _MISSING_SCHEMA_VERSION)
        if raw_schema_version is _MISSING_SCHEMA_VERSION:
            schema_version = _LEGACY_UPLOAD_JOB_SCHEMA_VERSION
        elif not isinstance(raw_schema_version, int) or isinstance(
            raw_schema_version, bool
        ):
            raise ValueError("upload job schema_version must be an integer")
        else:
            schema_version = raw_schema_version

        if schema_version not in {
            _LEGACY_UPLOAD_JOB_SCHEMA_VERSION,
            UPLOAD_JOB_SCHEMA_VERSION,
        }:
            raise ValueError(
                f"unsupported upload job schema_version: {schema_version}"
            )

        data["status"] = JobStatus(str(data["status"]))
        data["uploaded_parts"] = [
            UploadedPart.from_api(part) for part in data.get("uploaded_parts", [])
        ]
        return cls(**data)

    @property
    def resumable(self) -> bool:
        return self.file_size > 0 and self.status in {
            JobStatus.AUTHENTICATING,
            JobStatus.CREATING_UPLOAD,
            JobStatus.UPLOADING,
            JobStatus.FINALIZING,
            JobStatus.PROCESSING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


@dataclass(frozen=True)
class UploadOutcome:
    upload_id: str
    splat_id: str
    status: str
    edit_url: str
    viewer_url: str
    splat: dict[str, Any]


@dataclass(frozen=True)
class ProgressSnapshot:
    uploaded_bytes: int
    total_bytes: int
    fraction: float
    bytes_per_second: float
    eta_seconds: float | None
    completed_parts: int
    total_parts: int
