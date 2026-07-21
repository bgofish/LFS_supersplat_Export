"""Coordinates the LFS main thread with background SuperSplat uploads."""

from __future__ import annotations

import copy
import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import ApiClient, DEFAULT_BASE_URL
from .credentials import clear_token as clear_stored_token
from .credentials import load_token, save_token
from .errors import ConfigurationError, UploadCancelled
from .lfs_export import LfsExportAdapter, SceneSelection
from .models import JobStatus, ProgressSnapshot, UploadJob, UploadedPart, utc_now
from .storage import JobStore
from .uploader import MAX_FILE_SIZE, UploadCallbacks, UploadEngine


@dataclass
class ControllerState:
    active: bool = False
    phase: str = "idle"
    status_text: str = "Ready to upload."
    progress: float = 0.0
    progress_text: str = ""
    account_text: str = ""
    token_source: str = ""
    token_mask: str = "Not configured"
    token_configured: bool = False
    error: str = ""
    edit_url: str = ""
    viewer_url: str = ""
    splat_id: str = ""
    resumable_title: str = ""
    resumable_size: int = 0
    resume_available: bool = False
    scene_summary: str = ""


class PluginController:
    def __init__(self, lf: Any, runtime_state: Any, store: JobStore | None = None) -> None:
        self.lf = lf
        self.runtime_state = runtime_state
        self.adapter = LfsExportAdapter(lf, runtime_state)
        self.store = store or JobStore()
        self.settings = lf.plugins.settings("supersplat")
        self._lock = threading.RLock()
        self._state = ControllerState()
        self._job: UploadJob | None = None
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._export_seen_active = False
        self._closed = False
        self._remember_token = bool(self.settings.get("remember_token", False))
        self._token = os.environ.get("SUPERSPLAT_API_KEY", "").strip()
        if not self._token and self._remember_token:
            self._token = load_token()
        if self._token:
            self._state.token_source = (
                "SUPERSPLAT_API_KEY"
                if os.environ.get("SUPERSPLAT_API_KEY", "").strip()
                else "secure credential store"
            )
        self._update_token_mask()
        self.store.cleanup_completed()
        self._refresh_resume()
        self._unsubscribe = runtime_state.export_progress_state.subscribe(
            self._on_export_state
        )

    def snapshot(self) -> ControllerState:
        with self._lock:
            return copy.copy(self._state)

    def preference(self, key: str, default: Any) -> Any:
        return self.settings.get(key, default)

    def save_preferences(self, values: dict[str, Any]) -> None:
        self.settings.update(values)

    def set_token(self, value: str, *, remember: bool | None = None) -> None:
        with self._lock:
            self._token = value.strip()
            if remember is not None:
                self._remember_token = bool(remember)
                self.settings.set("remember_token", self._remember_token)
            if self._remember_token:
                save_token(self._token)
            else:
                clear_stored_token()
            self._state.token_source = (
                "secure credential store" if self._token and self._remember_token
                else ("this LFS session" if self._token else "")
            )
            self._state.error = ""
            self._update_token_mask()

    def clear_token(self) -> None:
        clear_stored_token()
        self.set_token("", remember=False)

    def set_remember_token(self, enabled: bool) -> None:
        with self._lock:
            self._remember_token = bool(enabled)
            self.settings.set("remember_token", self._remember_token)
            if self._remember_token and self._token:
                save_token(self._token)
            elif not self._remember_token:
                clear_stored_token()
            self._state.token_source = (
                "secure credential store" if self._token and self._remember_token
                else ("this LFS session" if self._token else "")
            )

    def test_connection(self) -> None:
        token = self._require_token()
        with self._lock:
            if self._state.active:
                return
            self._state.active = True
            self._state.phase = "auth"
            self._state.status_text = "Checking SuperSplat account..."
            self._state.error = ""

        def run() -> None:
            try:
                me = ApiClient(token, self.base_url).get_me()
                label = str(me.get("name") or me.get("username") or me.get("email") or me.get("id") or "Connected")
                with self._lock:
                    self._state.account_text = label
                    self._state.status_text = f"Connected as {label}."
            except Exception as error:
                self._set_error(error)
            finally:
                with self._lock:
                    self._state.active = False
                    self._state.phase = "idle"
                self.adapter.request_redraw()

        threading.Thread(target=run, daemon=True, name="supersplat-auth").start()

    @property
    def base_url(self) -> str:
        default = os.environ.get("SUPERSPLAT_BASE_URL", DEFAULT_BASE_URL)
        return str(self.preference("base_url", default)).rstrip("/")

    def inspect_scope(self, scope: str) -> SceneSelection:
        return self.adapter.resolve(scope)

    def suggested_title(self, scope: str) -> str:
        return self.adapter.suggested_title(scope)

    def start_upload(
        self,
        *,
        scope: str,
        source_format: str,
        title: str,
        description: str,
        sh_degree: int,
        concurrency: int,
    ) -> None:
        token = self._require_token()
        with self._lock:
            if self._state.active:
                raise ConfigurationError("An export or upload is already running.")
        selection = self.adapter.resolve(scope)
        actual_sh_degree = max(0, min(int(sh_degree), selection.maximum_sh_degree, 3))
        job_id = uuid.uuid4().hex
        path = self.store.staging_path(job_id, source_format)
        created_at = utc_now()
        idempotency = hashlib.sha256(
            f"{job_id}:{self.base_url}:{path}".encode("utf-8")
        ).hexdigest()
        job = UploadJob(
            id=job_id,
            created_at=created_at,
            updated_at=created_at,
            status=JobStatus.EXPORTING,
            file_path=str(path),
            source_format=source_format,
            title=title.strip() or self.adapter.suggested_title(scope),
            description=description.strip(),
            node_names=selection.node_names,
            sh_degree=actual_sh_degree,
            idempotency_key=f"supersplat-{idempotency}",
            base_url=self.base_url,
        )
        # Runtime-only attributes are deliberately excluded from the JSON manifest.
        setattr(job, "_api_key", token)
        setattr(job, "_concurrency", max(1, min(int(concurrency), 8)))
        self.store.save(job)
        with self._lock:
            self._job = job
            self._cancel_event = threading.Event()
            self._export_seen_active = False
            self._state.active = True
            self._state.phase = "export"
            self._state.status_text = f"Exporting {len(selection.node_names)} splat node(s) to {source_format.upper()}..."
            self._state.progress = 0.0
            self._state.progress_text = "Starting native export"
            self._state.error = ""
            self._state.edit_url = ""
            self._state.viewer_url = ""
        try:
            self.adapter.start_export(
                source_format, path, selection.node_names, actual_sh_degree
            )
        except Exception as error:
            self._finish_failure(error)
            raise

    def report_error(self, error: Exception) -> None:
        """Surface a synchronous panel action error without creating a failed job."""
        self._set_error(error)
        self.adapter.request_redraw()

    def resume_latest(self, concurrency: int) -> None:
        token = self._require_token()
        job = self.store.latest_resumable()
        if job is None:
            raise ConfigurationError("No staged upload is available to resume.")
        with self._lock:
            if self._state.active:
                raise ConfigurationError("An export or upload is already running.")
            self._job = job
            self._cancel_event = threading.Event()
            self._state.active = True
            self._state.phase = "upload"
            self._state.error = ""
            self._state.status_text = "Resuming upload..."
        setattr(job, "_api_key", token)
        setattr(job, "_concurrency", max(1, min(int(concurrency), 8)))
        self._start_upload_worker()

    def discard_resume(self) -> None:
        job = self.store.latest_resumable()
        if job is not None:
            self.store.delete(job)
        self._refresh_resume()

    def cancel(self) -> None:
        with self._lock:
            phase = self._state.phase
            self._state.status_text = "Cancelling..."
            self._cancel_event.set()
        if phase == "export":
            try:
                self.adapter.cancel_export()
            except Exception:
                pass

    def poll_export_state(self) -> None:
        with self._lock:
            exporting = self._state.active and self._state.phase == "export"
        if exporting:
            self._on_export_state(self.adapter.export_state())

    def open_url(self, url: str) -> None:
        self.adapter.open_url(url)

    def copy_text(self, text: str) -> None:
        self.adapter.copy_text(text)

    def shutdown(self) -> None:
        self._closed = True
        self.cancel()
        try:
            self._unsubscribe()
        except Exception:
            pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

    def _on_export_state(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        with self._lock:
            if not self._state.active or self._state.phase != "export" or self._job is None:
                return
            active = bool(value.get("active", False))
            stage = str(value.get("stage", ""))
            path = str(value.get("path", ""))
            if path and Path(path) != Path(self._job.file_path):
                return
            if active:
                self._export_seen_active = True
                progress = float(value.get("progress", 0.0) or 0.0)
                self._state.progress = max(0.0, min(progress, 1.0))
                self._state.progress_text = stage or "Exporting"
                self.adapter.request_redraw()
                return
            if not self._export_seen_active and stage != "Complete":
                return
            error_text = str(value.get("error", "") or "")
            job = self._job

        if self._cancel_event.is_set():
            self._finish_cancelled()
        elif error_text or stage != "Complete":
            self._finish_failure(ConfigurationError(error_text or "LichtFeld export failed."))
        else:
            try:
                size = Path(job.file_path).stat().st_size
                if size <= 0:
                    raise ConfigurationError("LichtFeld produced an empty export.")
                if size > MAX_FILE_SIZE:
                    raise ConfigurationError(
                        "This plugin currently limits uploads to 10 GiB."
                    )
                job.file_size = size
                job.status = JobStatus.AUTHENTICATING
                self.store.save(job)
                self._start_upload_worker()
            except Exception as error:
                self._finish_failure(error)

    def _start_upload_worker(self) -> None:
        with self._lock:
            if self._job is None:
                return
            job = self._job
            token = str(getattr(job, "_api_key", ""))
            concurrency = int(getattr(job, "_concurrency", 4))
            self._state.phase = "upload"
            self._state.progress = 0.0
            self._state.status_text = "Preparing upload..."
        engine = UploadEngine(concurrency=concurrency)

        def stage(message: str) -> None:
            with self._lock:
                self._state.status_text = message
                if "Finalizing" in message:
                    job.status = JobStatus.FINALIZING
                elif "Upload" in message or "upload" in message:
                    job.status = JobStatus.UPLOADING
                self.store.save(job)
            self.adapter.request_redraw()

        def account(me: dict[str, Any]) -> None:
            job.account_id = str(me.get("id", ""))
            job.account_name = str(me.get("name") or me.get("username") or me.get("email") or "")
            with self._lock:
                self._state.account_text = job.account_name or job.account_id
            self.store.save(job)

        def progress(value: ProgressSnapshot) -> None:
            from .progress import format_bytes, format_duration

            with self._lock:
                self._state.progress = value.fraction
                rate = format_bytes(value.bytes_per_second)
                self._state.progress_text = (
                    f"{format_bytes(value.uploaded_bytes)} / {format_bytes(value.total_bytes)}"
                    f"  •  {rate}/s  •  ETA {format_duration(value.eta_seconds)}"
                )
            self.adapter.request_redraw()

        def checkpoint(upload_id: str, parts: list[UploadedPart]) -> None:
            job.upload_id = upload_id
            job.uploaded_parts = list(parts)
            job.status = JobStatus.UPLOADING
            self.store.save(job)

        callbacks = UploadCallbacks(stage, account, progress, checkpoint)

        def run() -> None:
            try:
                outcome = engine.upload(job, token, self._cancel_event, callbacks)
                job.status = JobStatus.COMPLETE
                job.upload_id = outcome.upload_id
                job.splat_id = outcome.splat_id
                job.edit_url = outcome.edit_url
                job.viewer_url = outcome.viewer_url
                job.error = ""
                self.store.save(job)
                with self._lock:
                    self._state.active = False
                    self._state.phase = "complete"
                    self._state.status_text = "Upload accepted by SuperSplat."
                    self._state.progress = 1.0
                    self._state.edit_url = outcome.edit_url
                    self._state.viewer_url = outcome.viewer_url
                    self._state.splat_id = outcome.splat_id
                    self._state.error = ""
                self.store.delete(job)
            except UploadCancelled:
                self._finish_cancelled()
            except Exception as error:
                self._finish_failure(error)
            finally:
                if hasattr(job, "_api_key"):
                    delattr(job, "_api_key")
                self._refresh_resume()
                self.adapter.request_redraw()

        self._worker = threading.Thread(
            target=run, daemon=True, name="supersplat-upload"
        )
        self._worker.start()

    def _finish_cancelled(self) -> None:
        with self._lock:
            job = self._job
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.error = "Upload cancelled."
                self.store.save(job)
            self._state.active = False
            self._state.phase = "cancelled"
            self._state.status_text = (
                "Cancelled. The staged file can be resumed."
                if job is not None and job.file_size > 0
                else "Export cancelled."
            )
            self._state.error = ""
        self._refresh_resume()
        self.adapter.request_redraw()

    def _finish_failure(self, error: Exception) -> None:
        message = str(error) or error.__class__.__name__
        with self._lock:
            job = self._job
            if job is not None:
                job.status = JobStatus.FAILED
                job.error = message
                self.store.save(job)
            self._state.active = False
            self._state.phase = "failed"
            self._state.status_text = "Upload stopped."
            self._state.error = message
        self._refresh_resume()
        self.adapter.request_redraw()

    def _set_error(self, error: Exception) -> None:
        message = str(error) or error.__class__.__name__
        with self._lock:
            self._state.error = message
            self._state.status_text = "Connection failed."

    def _refresh_resume(self) -> None:
        job = self.store.latest_resumable()
        with self._lock:
            self._state.resume_available = job is not None
            self._state.resumable_title = job.title if job else ""
            self._state.resumable_size = job.file_size if job else 0

    def _require_token(self) -> str:
        with self._lock:
            token = self._token
        if not token:
            raise ConfigurationError(
                "Paste a SuperSplat API key, or set SUPERSPLAT_API_KEY before launching LFS."
            )
        return token

    def _update_token_mask(self) -> None:
        self._state.token_configured = bool(self._token)
        self._state.token_mask = (
            f"Configured (••••{self._token[-4:]})" if len(self._token) >= 4 else (
                "Configured" if self._token else "Not configured"
            )
        )
