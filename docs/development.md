# Development

## Local setup

Link your checkout into LichtFeld Studio's plugin directory instead of copying it after every change. The destination must not already exist.

### Windows

From the repository root in PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.lichtfeld\plugins" | Out-Null
New-Item -ItemType Junction `
  -Path "$HOME\.lichtfeld\plugins\supersplat" `
  -Target (Resolve-Path .)
```

### Linux

From the repository root:

```bash
mkdir -p ~/.lichtfeld/plugins
ln -s "$(pwd)" ~/.lichtfeld/plugins/supersplat
```

Enable the plugin and its watcher in LichtFeld Studio's Python Console:

```python
import lichtfeld as lf

lf.plugins.discover()
lf.plugins.settings("supersplat").set("load_on_startup", True)
lf.plugins.load("supersplat")
lf.plugins.start_watcher()
```

With `hot_reload = true` in `pyproject.toml`, saving a Python file reloads the active plugin. The watcher does not currently watch `.rml` or `.rcss` files, so touch a Python file after changing panel markup or styles:

```powershell
(Get-Item .\panels\main_panel.py).LastWriteTime = Get-Date
```

You can also reload the plugin explicitly:

```python
lf.plugins.reload("supersplat")
```

If reloading fails, inspect `lf.plugins.get_error("supersplat")`. See the [LichtFeld plugin developer guide](https://lichtfeld.io/docs/guide/#hot-reload-debugging) for host-level debugging details.

## Advanced configuration

The plugin supports the following environment variables and plugin setting:

| Setting | Purpose |
| --- | --- |
| `SUPERSPLAT_BASE_URL` | Overrides the default SuperSplat API base URL. |
| `SUPERSPLAT_CACHE_DIR` | Changes where staged exports and upload checkpoints are stored. |
| `base_url` plugin setting | Overrides the API base URL through LichtFeld Studio's plugin settings. |

If both API URL overrides are configured, the `base_url` plugin setting takes precedence over `SUPERSPLAT_BASE_URL`.

## Tests

Run the isolated test suite from the repository root:

```console
python -m unittest discover -s tests -v
```

The tests use fake HTTP servers and never modify the live SuperSplat service. Before release, run an authenticated upload and an interrupted/resumed upload inside LichtFeld Studio because the native `lichtfeld` module and exporter are provided by the host.
