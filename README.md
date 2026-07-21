# SuperSplat for LichtFeld Studio

Upload Gaussian splat scenes from [LichtFeld Studio](https://lichtfeld.io/) directly to [SuperSplat](https://superspl.at/). The plugin exports the splats in your scene and uploads them without leaving LichtFeld Studio.

<p align="center">
  <img src="docs/images/supersplat-upload-panel.png" alt="SuperSplat panel in LichtFeld Studio" width="338">
</p>

## Installation

Requires LichtFeld Studio 0.5.3 or later.

1. Open the **Plugins** panel.
2. Enter `https://github.com/playcanvas/supersplat-lichtfeld-plugin`.
3. Select **Install**.
4. Select **Load** if the plugin is not activated automatically. Optionally enable **Load on Startup**.

The **SuperSplat** tab will appear in the main workspace.

## Usage

SuperSplat uses PlayCanvas accounts, so your PlayCanvas account is also your SuperSplat account.

### Create a PlayCanvas API key

1. Sign in to your [PlayCanvas account](https://playcanvas.com/account).
2. Under **API Tokens**, select **Generate Token** and give the token a name.
3. Copy the token when it is displayed. You will not be able to view it again after closing the window.
4. Open the **SuperSplat** tab in LichtFeld Studio and paste the token into the **PlayCanvas API key** field.
5. Optionally enable **Store securely with your operating system**, then select **Connect**.

PlayCanvas calls this credential an API token; use it as the API key in the plugin. The plugin only remembers it when secure credential storage is available and never saves it as plain text.

### Upload a scene

1. Open a scene containing at least one splat node.
2. In the **SuperSplat** tab, choose **All splats** or **Visible splats**.
3. Enter a title and, optionally, a description.
4. If needed, open **Advanced** to choose the file format, SH degree, and number of parallel upload parts.
5. Select **Upload to SuperSplat**.

After the upload completes, use the links in the panel to open the scene in the SuperSplat editor or viewer. If an upload is interrupted after export, the panel lets you resume it from the staged local file or discard it.

## Development

For local development, testing, and advanced configuration, see the [development guide](docs/development.md).
