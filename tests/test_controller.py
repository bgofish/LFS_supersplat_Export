from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lfs_supersplat.controller import PluginController
from lfs_supersplat.models import JobStatus, UploadJob, UploadOutcome, utc_now
from lfs_supersplat.storage import JobStore


class _Settings:
    def __init__(self) -> None:
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value

    def update(self, values) -> None:
        self.values.update(values)


class _Plugins:
    def __init__(self) -> None:
        self.value = _Settings()

    def settings(self, _name):
        return self.value


class _Lfs:
    def __init__(self) -> None:
        self.plugins = _Plugins()


class _Signal:
    value = {}

    def subscribe(self, _callback):
        return lambda: None


class _RuntimeState:
    export_progress_state = _Signal()


def _job(store: JobStore, job_id: str, status: JobStatus) -> tuple[UploadJob, Path]:
    staged = store.staging_path(job_id, "ply")
    staged.write_bytes(b"staged export")
    now = utc_now()
    job = UploadJob(
        id=job_id, created_at=now, updated_at=now, status=status,
        file_path=str(staged), source_format="ply", title="Scene", description="",
        node_names=["Splat"], sh_degree=3, idempotency_key=f"key-{job_id}",
        base_url="https://example.test", file_size=staged.stat().st_size,
    )
    store.save(job)
    return job, staged


class PluginControllerCleanupTests(unittest.TestCase):
    def test_startup_removes_completed_leftovers_and_preserves_failed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            completed, completed_path = _job(store, "completed", JobStatus.COMPLETE)
            failed, failed_path = _job(store, "failed", JobStatus.FAILED)

            controller = PluginController(_Lfs(), _RuntimeState(), store)
            try:
                self.assertFalse(completed_path.exists())
                self.assertIsNone(store.load(completed.id))
                self.assertTrue(failed_path.exists())
                self.assertIsNotNone(store.load(failed.id))
                self.assertTrue(controller.snapshot().resume_available)
            finally:
                controller.shutdown()

    def test_successful_upload_removes_staged_file_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job, staged = _job(store, "uploading", JobStatus.UPLOADING)
            controller = PluginController(_Lfs(), _RuntimeState(), store)
            controller._job = job
            controller._token = "secret"
            setattr(job, "_api_key", "secret")
            setattr(job, "_concurrency", 1)
            outcome = UploadOutcome(
                upload_id="upload-1", splat_id="splat-1", status="completed",
                edit_url="https://editor.test/splat-1",
                viewer_url="https://viewer.test/splat-1", splat={},
            )

            with patch("lfs_supersplat.controller.UploadEngine") as engine_type:
                engine_type.return_value.upload.return_value = outcome
                controller._start_upload_worker()
                controller._worker.join(timeout=2)  # type: ignore[union-attr]

            try:
                state = controller.snapshot()
                self.assertEqual(state.phase, "complete")
                self.assertEqual(state.edit_url, outcome.edit_url)
                self.assertFalse(staged.exists())
                self.assertIsNone(store.load(job.id))
            finally:
                controller.shutdown()


if __name__ == "__main__":
    unittest.main()
