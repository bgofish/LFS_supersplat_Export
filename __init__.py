"""LichtFeld SuperSplat plugin entry point."""

import lichtfeld as lf
from lfs_plugins.ui import RuntimeState

from .lfs_supersplat.runtime import start_controller, stop_controller
from .panels.main_panel import SuperSplatPanel

_PLUGIN_NAME = "lfs_supersplat"
_CLASSES = [SuperSplatPanel]
__lfs_panel_classes__ = ["SuperSplatPanel"]
__lfs_panel_ids__ = [SuperSplatPanel.id]


def on_load() -> None:
    start_controller(lf, RuntimeState)
    for cls in _CLASSES:
        lf.register_class(cls)
        # A panel can retain a disabled override across dev-plugin reloads.
        # Re-enable it on load so the plugin is discoverable in the main tab bar.
        if hasattr(lf.ui, "set_panel_enabled"):
            lf.ui.set_panel_enabled(cls.id, True)
    panel = lf.ui.get_panel(SuperSplatPanel.id) if hasattr(lf.ui, "get_panel") else None
    if panel is None:
        raise RuntimeError(f"LichtFeld did not register panel {SuperSplatPanel.id!r}")
    lf.log.info(
        f"{_PLUGIN_NAME} panel registered: id={panel.id} "
        f"space={panel.space.name if hasattr(panel.space, 'name') else panel.space} "
        f"enabled={panel.enabled}"
    )
    if hasattr(lf.ui, "request_redraw"):
        lf.ui.request_redraw()
    lf.log.info(f"{_PLUGIN_NAME} loaded")


def on_unload() -> None:
    for cls in reversed(_CLASSES):
        lf.unregister_class(cls)
    stop_controller()
    if hasattr(lf.ui, "request_redraw"):
        lf.ui.request_redraw()
    lf.log.info(f"{_PLUGIN_NAME} unloaded")
