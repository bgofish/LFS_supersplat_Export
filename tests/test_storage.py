from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lfs_supersplat.models import (
    UPLOAD_JOB_SCHEMA_VERSION,
    JobStatus,
    UploadJob,
    utc_now,
)
from lfs_supersplat.storage import JobStore


class JobStoreTests(unittest.TestCase):
    def test_round_trip_and_resumable_file_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            staged = store.staging_path("one", "ply")
            staged.write_bytes(b"12345")
            now = utc_now()
            job = UploadJob(
                id="one", created_at=now, updated_at=now, status=JobStatus.FAILED,
                file_path=str(staged), source_format="ply", title="Scene", description="",
                node_names=["Splat"], sh_degree=3, idempotency_key="key",
                base_url="https://example.test", file_size=5,
            )
            store.save(job)
            manifest = json.loads(
                (store.jobs_dir / "one.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["schema_version"], UPLOAD_JOB_SCHEMA_VERSION
            )
            loaded = store.load("one")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "Scene")  # type: ignore[union-attr]
            self.assertEqual(store.latest_resumable().id, "one")  # type: ignore[union-attr]
            staged.write_bytes(b"changed")
            self.assertIsNone(store.latest_resumable())

    def test_unversioned_manifest_remains_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            store.ensure()
            now = utc_now()
            job = UploadJob(
                id="legacy", created_at=now, updated_at=now,
                status=JobStatus.FAILED, file_path="/tmp/scene.ply",
                source_format="ply", title="Legacy", description="",
                node_names=["Splat"], sh_degree=3, idempotency_key="key",
                base_url="https://example.test", file_size=5,
            )
            manifest = job.to_dict()
            manifest.pop("schema_version")
            (store.jobs_dir / "legacy.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            loaded = store.load("legacy")

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, "Legacy")  # type: ignore[union-attr]

    def test_future_manifest_version_is_rejected_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            store.ensure()
            now = utc_now()
            job = UploadJob(
                id="future", created_at=now, updated_at=now,
                status=JobStatus.FAILED, file_path="/tmp/scene.ply",
                source_format="ply", title="Future", description="",
                node_names=["Splat"], sh_degree=3, idempotency_key="key",
                base_url="https://example.test", file_size=5,
            )
            manifest = job.to_dict()
            manifest["schema_version"] = UPLOAD_JOB_SCHEMA_VERSION + 1
            path = store.jobs_dir / "future.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertIsNone(store.load("future"))
            self.assertTrue(path.is_file())

    def test_runtime_credentials_are_not_serialized_in_manifests(self) -> None:
        now = utc_now()
        job = UploadJob(
            id="secret", created_at=now, updated_at=now, status=JobStatus.FAILED,
            file_path="/tmp/scene.ply", source_format="ply", title="Scene", description="",
            node_names=["Splat"], sh_degree=3, idempotency_key="key",
            base_url="https://example.test", file_size=5,
        )
        job._api_key = "do-not-write"  # type: ignore[attr-defined]
        self.assertNotIn("do-not-write", str(job.to_dict()))

    def test_cleanup_completed_deletes_only_completed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            now = utc_now()
            jobs = []
            for job_id, status in (
                ("complete", JobStatus.COMPLETE),
                ("failed", JobStatus.FAILED),
            ):
                staged = store.staging_path(job_id, "ply")
                staged.write_bytes(job_id.encode("ascii"))
                job = UploadJob(
                    id=job_id, created_at=now, updated_at=now, status=status,
                    file_path=str(staged), source_format="ply", title=job_id,
                    description="", node_names=["Splat"], sh_degree=3,
                    idempotency_key=f"key-{job_id}", base_url="https://example.test",
                    file_size=staged.stat().st_size,
                )
                store.save(job)
                jobs.append((job, staged))

            self.assertEqual(store.cleanup_completed(), 1)

            complete, complete_path = jobs[0]
            failed, failed_path = jobs[1]
            self.assertFalse(complete_path.exists())
            self.assertIsNone(store.load(complete.id))
            self.assertTrue(failed_path.exists())
            self.assertIsNotNone(store.load(failed.id))

    def test_delete_never_unlinks_a_file_outside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JobStore(root / "cache")
            outside = root / "outside.ply"
            outside.write_bytes(b"keep")
            now = utc_now()
            job = UploadJob(
                id="outside", created_at=now, updated_at=now,
                status=JobStatus.COMPLETE, file_path=str(outside),
                source_format="ply", title="Outside", description="",
                node_names=["Splat"], sh_degree=3, idempotency_key="key",
                base_url="https://example.test", file_size=4,
            )
            store.save(job)

            store.delete(job)

            self.assertTrue(outside.exists())
            self.assertIsNone(store.load(job.id))


if __name__ == "__main__":
    unittest.main()
