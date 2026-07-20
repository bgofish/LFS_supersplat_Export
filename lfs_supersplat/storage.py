"""Atomic local persistence for staged files and resumable upload jobs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import JobStatus, UploadJob


def default_cache_root() -> Path:
    override = os.environ.get("LFS_SUPERSPLAT_CACHE_DIR")
    if override:
        return Path(override).expanduser()

    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "LichtFeld" / "SuperSplat"

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "lichtfeld-supersplat"

    return Path.home() / ".cache" / "lichtfeld-supersplat"


class JobStore:
    """Stores one JSON manifest per upload and its staged export files."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.jobs_dir = self.root / "jobs"
        self.staging_dir = self.root / "staging"
        self._lock = threading.RLock()

    def ensure(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def staging_path(self, job_id: str, source_format: str) -> Path:
        self.ensure()
        return self.staging_dir / f"{job_id}.{source_format}"

    def save(self, job: UploadJob) -> None:
        self.ensure()
        job.touch()
        target = self.jobs_dir / f"{job.id}.json"
        payload = json.dumps(job.to_dict(), indent=2, sort_keys=True)

        with self._lock:
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{job.id}.", suffix=".tmp", dir=self.jobs_dir
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def load(self, job_id: str) -> UploadJob | None:
        path = self.jobs_dir / f"{job_id}.json"
        if not path.is_file():
            return None
        with self._lock:
            try:
                return UploadJob.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, KeyError):
                return None

    def list_jobs(self) -> list[UploadJob]:
        if not self.jobs_dir.is_dir():
            return []
        jobs: list[UploadJob] = []
        for path in self.jobs_dir.glob("*.json"):
            job = self.load(path.stem)
            if job is not None:
                jobs.append(job)
        return sorted(jobs, key=lambda item: item.updated_at, reverse=True)

    def latest_resumable(self) -> UploadJob | None:
        for job in self.list_jobs():
            if job.resumable and Path(job.file_path).is_file():
                try:
                    if Path(job.file_path).stat().st_size == job.file_size:
                        return job
                except OSError:
                    continue
        return None

    def cleanup_completed(self) -> int:
        """Delete staged files and manifests left by completed uploads."""
        completed = [
            job for job in self.list_jobs() if job.status == JobStatus.COMPLETE
        ]
        for job in completed:
            self.delete(job)
        return len(completed)

    def delete(self, job: UploadJob, *, delete_staging: bool = True) -> None:
        with self._lock:
            if delete_staging:
                try:
                    staged_path = Path(job.file_path)
                    staged_path.resolve().relative_to(self.staging_dir.resolve())
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
                except ValueError:
                    # Never delete a path outside this store's staging directory.
                    pass
            try:
                (self.jobs_dir / f"{job.id}.json").unlink(missing_ok=True)
            except OSError:
                pass
