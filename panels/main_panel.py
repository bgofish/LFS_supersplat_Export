"""Retained-mode SuperSplat upload panel."""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

import lichtfeld as lf

from ..supersplat.camera_path import CameraPathExporter, LOOP_MODES
from ..supersplat.errors import SuperSplatError
from ..supersplat.progress import format_bytes
from ..supersplat.runtime import get_controller


# TODO: Add a selected-splats scope once selection export can be made reliable.
SCOPES = ("all", "visible")
FORMATS = ("ply", "sog")
CP_LOOP_LABELS = ("repeat", "once", "pingpong")  # UI labels; LOOP_MODES has the JSON values
PLAYCANVAS_ACCOUNT_URL = "https://playcanvas.com/account"
PLUGIN_GITHUB_URL = "https://github.com/playcanvas/supersplat-lichtfeld-plugin"

_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _xml_unescape(value) -> str:
    return html.unescape(str(value or ""))


def _open_dialog(title: str, file_filter: str):
    if sys.platform != "win32":
        return None
    ps_script = f'''
    Add-Type -AssemblyName System.Windows.Forms
    $d = New-Object System.Windows.Forms.OpenFileDialog
    $d.Title = "{title}"
    $d.Filter = "{file_filter}"
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
        Write-Output $d.FileName
    }}
    '''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, creationflags=_SUBPROCESS_FLAGS,
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception:
        return None


def _save_dialog(title: str, file_filter: str, default_name: str = ""):
    if sys.platform != "win32":
        return None
    ps_script = f'''
    Add-Type -AssemblyName System.Windows.Forms
    $d = New-Object System.Windows.Forms.SaveFileDialog
    $d.Title = "{title}"
    $d.Filter = "{file_filter}"
    $d.FileName = "{default_name}"
    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {{
        Write-Output $d.FileName
    }}
    '''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, creationflags=_SUBPROCESS_FLAGS,
        )
        path = result.stdout.strip()
        return path if path else None
    except Exception:
        return None


class SuperSplatPanel(lf.ui.Panel):
    id = "supersplat.main"
    label = "SuperSplat"
    space = lf.ui.PanelSpace.MAIN_PANEL_TAB
    order = 210
    template = str(Path(__file__).resolve().with_name("main_panel.rml"))
    height_mode = lf.ui.PanelHeightMode.FILL
    update_interval_ms = 100
    update_policy = "interval"

    def __init__(self) -> None:
        controller = get_controller()
        scope_index = int(controller.preference("scope_index", 0))
        if scope_index not in range(len(SCOPES)):
            scope_index = 0
        format_index = max(0, min(int(controller.preference("format_index", 0)), 1))
        sh_index = max(0, min(int(controller.preference("sh_index", 3)), 3))

        self.scope = SCOPES[scope_index]
        self.source_format = FORMATS[format_index]
        self.sh_degree = str(sh_index)
        self.concurrency = str(
            max(1, min(int(controller.preference("concurrency", 4)), 8))
        )
        self.title = controller.suggested_title(self.scope)
        self.description = ""
        self.api_key_entry = ""
        self.remember_token = bool(controller.preference("remember_token", False))

        state = controller.snapshot()
        self._credentials_open = not state.token_configured
        self._advanced_open = False
        self._selection_valid = False
        self._selection_title = "Checking scene..."
        self._selection_note = ""
        self._selection_metadata = ""
        self._last_scope = ""
        self._last_state_key = None
        self._auth_pending = False
        self._dismissed_splat_id = ""
        self._handle = None
        self._doc = None

        self._cp_exporter = CameraPathExporter(lf)
        self._cp_json_path = ""
        self._cp_sequencer_data = None
        self._cp_sequencer_kf_count = 0
        self._cp_loop_idx = 0
        self._cp_smoothness = "0.5"
        self._cp_fps = "30"
        self._cp_status = ""
        self._cp_last_output = ""

        self._refresh_selection(force=True)

    # -- Data model -----------------------------------------------------

    def on_bind_model(self, ctx) -> None:
        model = ctx.create_data_model("supersplat")
        if model is None:
            return

        for name, getter, setter in (
            ("scope", lambda: self.scope, self._set_scope),
            ("source_format", lambda: self.source_format, self._set_source_format),
            ("sh_degree", lambda: self.sh_degree, self._set_sh_degree),
            ("concurrency", lambda: self.concurrency, self._set_concurrency),
            ("title", lambda: self.title, self._set_title),
            ("description", lambda: self.description, self._set_description),
            ("api_key_entry", lambda: self.api_key_entry, self._set_api_key_entry),
        ):
            model.bind(name, getter, setter)

        model.bind_func("show_form", self._show_form)
        model.bind_func("show_progress", self._show_progress)
        model.bind_func("show_success", self._show_success)
        model.bind_func("show_account", self._show_account)
        model.bind_func("show_credentials", lambda: not self._show_account())
        model.bind_func("show_cancel_credentials", self._token_configured)
        model.bind_func("can_connect", self._can_connect)
        model.bind_func("account_title", self._account_title)
        model.bind_func("account_source", self._account_source)
        model.bind_func("remember_token", lambda: self.remember_token)
        model.bind_func("advanced_open", lambda: self._advanced_open)
        model.bind_func("advanced_marker", lambda: "v" if self._advanced_open else ">")
        model.bind_func("selection_valid", lambda: self._selection_valid)
        model.bind_func("selection_title", lambda: self._selection_title)
        model.bind_func("selection_note", lambda: self._selection_note)
        model.bind_func("can_upload", self._can_upload)
        model.bind_func("show_credentials_error", self._show_credentials_error)
        model.bind_func("show_upload_error", self._show_upload_error)
        model.bind_func("form_error", lambda: get_controller().snapshot().error)
        model.bind_func("show_resume", self._show_resume)
        model.bind_func("resume_title", lambda: get_controller().snapshot().resumable_title)
        model.bind_func("resume_size", self._resume_size)
        model.bind_func("progress_heading", self._progress_heading)
        model.bind_func("progress_status", lambda: get_controller().snapshot().status_text)
        model.bind_func("progress_value", self._progress_value)
        model.bind_func("progress_pct", self._progress_pct)
        model.bind_func("progress_detail", lambda: get_controller().snapshot().progress_text)
        model.bind_func("success_title", self._success_title)
        model.bind_func("success_metadata", lambda: self._selection_metadata)
        model.bind_func("has_edit_url", lambda: bool(get_controller().snapshot().edit_url))
        model.bind_func("has_viewer_url", lambda: bool(get_controller().snapshot().viewer_url))

        model.bind(
            "cp_loop_idx",
            lambda: str(self._cp_loop_idx),
            self._set_cp_loop_idx,
        )
        model.bind(
            "cp_smoothness",
            lambda: self._cp_smoothness,
            self._set_cp_smoothness,
        )
        model.bind(
            "cp_fps",
            lambda: self._cp_fps,
            self._set_cp_fps,
        )
        model.bind_func("cp_has_source", self._cp_has_source)
        model.bind_func("cp_source_label", self._cp_source_label)
        model.bind_func("cp_can_export", self._cp_has_source)
        model.bind_func("cp_has_status", lambda: bool(self._cp_status))
        model.bind_func("cp_status_text", lambda: self._cp_status)
        model.bind_func("cp_status_ok", lambda: "exported" in self._cp_status.lower())
        model.bind_func("cp_status_error", lambda: any(
            w in self._cp_status.lower() for w in ("failed", "error")
        ))

        for name, handler in (
            ("change_credentials", self._on_change_credentials),
            ("cancel_credentials", self._on_cancel_credentials),
            ("connect", self._on_connect),
            ("open_playcanvas_account", self._on_open_playcanvas_account),
            ("open_plugin_github", self._on_open_plugin_github),
            ("forget_key", self._on_forget_key),
            ("toggle_remember", self._on_toggle_remember),
            ("toggle_advanced", self._on_toggle_advanced),
            ("start_upload", self._on_start_upload),
            ("cancel_upload", self._on_cancel_upload),
            ("resume_upload", self._on_resume_upload),
            ("discard_resume", self._on_discard_resume),
            ("open_editor", self._on_open_editor),
            ("open_viewer", self._on_open_viewer),
            ("copy_link", self._on_copy_link),
            ("upload_another", self._on_upload_another),
            ("cp_browse_file", self._on_cp_browse_file),
            ("cp_load_sequencer", self._on_cp_load_sequencer),
            ("cp_export", self._on_cp_export),
        ):
            model.bind_event(name, handler)

        self._handle = model.get_handle()

    # -- Lifecycle ------------------------------------------------------

    def on_mount(self, doc) -> None:
        super().on_mount(doc)
        self._doc = doc
        self._refresh_selection(force=True)
        self._dirty_model()

    def on_update(self, doc) -> bool:
        del doc
        controller = get_controller()
        controller.poll_export_state()
        state = controller.snapshot()

        dirty = self._refresh_selection()
        state_key = tuple(vars(state).values())
        if state_key != self._last_state_key:
            self._last_state_key = state_key
            self._dirty_model()
            dirty = True
        if self._auth_pending and not state.active:
            self._auth_pending = False
            if state.token_configured and not state.error:
                self._credentials_open = False
                self.api_key_entry = ""
            self._dirty_model()
            dirty = True
        return dirty

    def on_scene_changed(self, doc) -> None:
        del doc
        self._last_scope = ""

    def on_unmount(self, doc) -> None:
        doc.remove_data_model("supersplat")
        self._handle = None
        self._doc = None

    # -- Bound state ----------------------------------------------------

    def _set_scope(self, value) -> None:
        value = str(value)
        if value not in SCOPES or value == self.scope:
            return
        self.scope = value
        if not self.title.strip():
            self.title = get_controller().suggested_title(value)
        self._refresh_selection(force=True)
        self._dirty_model()

    def _set_source_format(self, value) -> None:
        value = str(value).lower()
        if value in FORMATS and value != self.source_format:
            self.source_format = value
            self._refresh_selection(force=True)
            self._dirty_model("source_format", "success_metadata")

    def _set_sh_degree(self, value) -> None:
        try:
            self.sh_degree = str(max(0, min(int(float(value)), 3)))
        except (TypeError, ValueError):
            pass

    def _set_concurrency(self, value) -> None:
        try:
            self.concurrency = str(max(1, min(int(float(value)), 8)))
        except (TypeError, ValueError):
            pass

    def _set_title(self, value) -> None:
        self.title = _xml_unescape(value)

    def _set_description(self, value) -> None:
        self.description = _xml_unescape(value)

    def _set_api_key_entry(self, value) -> None:
        self.api_key_entry = _xml_unescape(value)
        self._dirty_model("can_connect")

    def _set_cp_loop_idx(self, value) -> None:
        try:
            idx = max(0, min(int(float(value)), len(LOOP_MODES) - 1))
        except (TypeError, ValueError):
            return
        self._cp_loop_idx = idx

    def _set_cp_smoothness(self, value) -> None:
        try:
            v = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        self._cp_smoothness = f"{v:.3g}"

    def _set_cp_fps(self, value) -> None:
        self._cp_fps = str(value).strip()

    # -- View helpers ---------------------------------------------------

    def _show_form(self) -> bool:
        return not self._show_progress() and not self._show_success()

    @staticmethod
    def _show_progress() -> bool:
        state = get_controller().snapshot()
        return state.active and state.phase in {"export", "upload"}

    def _show_success(self) -> bool:
        state = get_controller().snapshot()
        return bool(
            state.phase == "complete"
            and (state.edit_url or state.viewer_url)
            and state.splat_id != self._dismissed_splat_id
        )

    def _show_account(self) -> bool:
        return self._token_configured() and not self._credentials_open

    @staticmethod
    def _token_configured() -> bool:
        return get_controller().snapshot().token_configured

    @staticmethod
    def _account_title() -> str:
        account = get_controller().snapshot().account_text
        return f"Connected as {account}" if account else "Connected to SuperSplat"

    @staticmethod
    def _account_source() -> str:
        source = get_controller().snapshot().token_source
        return {
            "secure credential store": "Stored securely",
            "this LFS session": "PlayCanvas API key for this session",
        }.get(source, source)

    def _can_upload(self) -> bool:
        state = get_controller().snapshot()
        return state.token_configured and self._selection_valid and not state.active

    def _can_connect(self) -> bool:
        return bool(
            self.api_key_entry.strip() and not get_controller().snapshot().active
        )

    def _show_credentials_error(self) -> bool:
        state = get_controller().snapshot()
        return bool(state.error and not state.active and self._credentials_open)

    def _show_upload_error(self) -> bool:
        state = get_controller().snapshot()
        return bool(state.error and not state.active and not self._credentials_open)

    @staticmethod
    def _show_resume() -> bool:
        state = get_controller().snapshot()
        return state.resume_available and not state.active

    @staticmethod
    def _resume_size() -> str:
        return format_bytes(get_controller().snapshot().resumable_size)

    @staticmethod
    def _progress_heading() -> str:
        if get_controller().snapshot().phase == "export":
            return "Exporting scene"
        return "Uploading scene"

    @staticmethod
    def _progress_value() -> str:
        return str(max(0.0, min(get_controller().snapshot().progress, 1.0)))

    @staticmethod
    def _progress_pct() -> str:
        return f"{get_controller().snapshot().progress * 100:.0f}%"

    def _success_title(self) -> str:
        return self.title.strip() or "LichtFeld scene"

    def _cp_has_source(self) -> bool:
        return bool(self._cp_json_path) or bool(self._cp_sequencer_data)

    def _cp_source_label(self) -> str:
        if self._cp_sequencer_data is not None:
            return f"From sequencer ({self._cp_sequencer_kf_count} keyframes)"
        if self._cp_json_path:
            return Path(self._cp_json_path).name
        return "No camera path selected"

    def _refresh_selection(self, *, force: bool = False) -> bool:
        if not force and self.scope == self._last_scope:
            return False
        previous = (
            self._selection_valid,
            self._selection_title,
            self._selection_note,
            self._selection_metadata,
        )
        self._last_scope = self.scope
        try:
            selection = get_controller().inspect_scope(self.scope)
            count = len(selection.node_names)
            noun = "splat node" if count == 1 else "splat nodes"
            self._selection_valid = True
            self._selection_title = f"{count} {noun} ready"
            self._selection_note = f"{selection.gaussian_count:,} Gaussians"
            self._selection_metadata = (
                f"{self.source_format.upper()} - {selection.gaussian_count:,} Gaussians"
            )
        except SuperSplatError as error:
            labels = {
                "all": "No splats in scene",
                "visible": "No visible splats",
            }
            self._selection_valid = False
            self._selection_title = labels.get(self.scope, "Scene is not ready")
            self._selection_note = str(error)
            self._selection_metadata = ""
        current = (
            self._selection_valid,
            self._selection_title,
            self._selection_note,
            self._selection_metadata,
        )
        if current != previous:
            self._dirty_model(
                "selection_valid",
                "selection_title",
                "selection_note",
                "can_upload",
                "success_metadata",
            )
            return True
        return False

    # -- Events ---------------------------------------------------------

    def _on_change_credentials(self, _handle, _event, _args) -> None:
        self._credentials_open = True
        self._dirty_model("show_account", "show_credentials")

    def _on_cancel_credentials(self, _handle, _event, _args) -> None:
        if self._token_configured():
            self._credentials_open = False
            self.api_key_entry = ""
            self._dirty_model()

    def _on_connect(self, _handle, _event, _args) -> None:
        if not self._can_connect():
            return
        controller = get_controller()
        controller.set_token(self.api_key_entry, remember=self.remember_token)
        self.api_key_entry = ""
        self._auth_pending = True
        self._call(controller.test_connection)
        self._dirty_model()

    def _on_forget_key(self, _handle, _event, _args) -> None:
        get_controller().clear_token()
        self._credentials_open = True
        self.api_key_entry = ""
        self._dirty_model()

    def _on_open_playcanvas_account(self, _handle, _event, _args) -> None:
        get_controller().open_url(PLAYCANVAS_ACCOUNT_URL)

    def _on_open_plugin_github(self, _handle, _event, _args) -> None:
        get_controller().open_url(PLUGIN_GITHUB_URL)

    def _on_toggle_remember(self, _handle, event, _args) -> None:
        checkbox = event.current_target() if event is not None else None
        if checkbox is None:
            return
        enabled = checkbox.has_attribute("checked")
        if enabled == self.remember_token:
            return
        self.remember_token = enabled
        get_controller().set_remember_token(enabled)
        self._dirty_model("account_source")

    def _on_toggle_advanced(self, _handle, _event, _args) -> None:
        self._advanced_open = not self._advanced_open
        self._dirty_model("advanced_open", "advanced_marker")

    def _on_start_upload(self, _handle, _event, _args) -> None:
        if not self._can_upload():
            return
        controller = get_controller()
        controller.save_preferences(
            {
                "scope_index": SCOPES.index(self.scope),
                "format_index": FORMATS.index(self.source_format),
                "sh_index": int(self.sh_degree),
                "concurrency": int(self.concurrency),
            }
        )
        self._dismissed_splat_id = ""
        self._call(
            lambda: controller.start_upload(
                scope=self.scope,
                source_format=self.source_format,
                title=self.title,
                description=self.description,
                sh_degree=int(self.sh_degree),
                concurrency=int(self.concurrency),
            )
        )
        self._dirty_model()

    def _on_cancel_upload(self, _handle, _event, _args) -> None:
        get_controller().cancel()

    def _on_resume_upload(self, _handle, _event, _args) -> None:
        self._call(lambda: get_controller().resume_latest(int(self.concurrency)))

    def _on_discard_resume(self, _handle, _event, _args) -> None:
        get_controller().discard_resume()
        self._dirty_model()

    def _on_open_editor(self, _handle, _event, _args) -> None:
        state = get_controller().snapshot()
        if state.edit_url:
            get_controller().open_url(state.edit_url)

    def _on_open_viewer(self, _handle, _event, _args) -> None:
        state = get_controller().snapshot()
        if state.viewer_url:
            get_controller().open_url(state.viewer_url)

    def _on_copy_link(self, _handle, _event, _args) -> None:
        state = get_controller().snapshot()
        url = state.edit_url or state.viewer_url
        if url:
            get_controller().copy_text(url)

    def _on_upload_another(self, _handle, _event, _args) -> None:
        self._dismissed_splat_id = get_controller().snapshot().splat_id
        self._refresh_selection(force=True)
        self._dirty_model()

    def _on_cp_browse_file(self, _handle, _event, _args) -> None:
        path = _open_dialog("Select camera_path.json", "JSON files (*.json)|*.json")
        if path:
            self._cp_json_path = path
            self._cp_sequencer_data = None
            self._cp_sequencer_kf_count = 0
            self._cp_status = ""
        self._dirty_model(
            "cp_source_label", "cp_has_source", "cp_can_export",
            "cp_has_status", "cp_status_text", "cp_status_ok", "cp_status_error",
        )

    def _on_cp_load_sequencer(self, _handle, _event, _args) -> None:
        try:
            data = self._cp_exporter.read_from_sequencer()
            kfs = data.get("keyframes", [])
            if not kfs:
                self._cp_status = "Sequencer has no keyframes to load."
            else:
                self._cp_sequencer_data = data
                self._cp_sequencer_kf_count = len(kfs)
                self._cp_json_path = ""
                self._cp_status = f"Loaded {len(kfs)} keyframes from sequencer."
        except Exception as error:
            lf.log.error(f"SuperSplat camera path: {error}")
            self._cp_status = f"Failed to read sequencer: {error}"
        self._dirty_model(
            "cp_source_label", "cp_has_source", "cp_can_export",
            "cp_has_status", "cp_status_text", "cp_status_ok", "cp_status_error",
        )

    def _on_cp_export(self, _handle, _event, _args) -> None:
        if not self._cp_has_source():
            self._cp_status = "Select a camera path first."
            self._dirty_model("cp_has_status", "cp_status_text", "cp_status_ok", "cp_status_error")
            return
        try:
            camera_path = self._cp_sequencer_data or self._cp_exporter.read_from_file(self._cp_json_path)
            fps = float(self._cp_fps) if self._cp_fps else 30.0
            smoothness = float(self._cp_smoothness) if self._cp_smoothness else 0.5
            name = self.title.strip() or "camera_path"
            data = self._cp_exporter.build_json(
                camera_path, name=name, loop_mode=LOOP_MODES[self._cp_loop_idx],
                smoothness=smoothness, fps=fps,
            )
            default_name = f"{name}_camera_path.json"
            out_path = _save_dialog("Export SuperSplat Animation JSON", "JSON files (*.json)|*.json", default_name)
            if not out_path:
                return  # cancelled
            self._cp_exporter.save_json(out_path, data)
            self._cp_last_output = out_path
            n_kf = len(data["animTracks"][0]["keyframes"]["times"])
            self._cp_status = f"Exported {Path(out_path).name} ({n_kf} keyframes)"
        except Exception as error:
            lf.log.error(f"SuperSplat camera path export: {error}")
            self._cp_status = f"Export failed: {error}"
        self._dirty_model("cp_has_status", "cp_status_text", "cp_status_ok", "cp_status_error")

    def _dirty_model(self, *fields: str) -> None:
        if not self._handle:
            return
        if not fields:
            self._handle.dirty_all()
            return
        for field in fields:
            self._handle.dirty(field)

    @staticmethod
    def _call(operation) -> None:
        try:
            operation()
        except Exception as error:
            lf.log.error(f"SuperSplat: {error}")
            get_controller().report_error(error)
