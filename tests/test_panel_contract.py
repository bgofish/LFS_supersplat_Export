from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
RML_PATH = ROOT / "panels" / "main_panel.rml"
RCSS_PATH = ROOT / "panels" / "main_panel.rcss"
PANEL_PATH = ROOT / "panels" / "main_panel.py"
LOGO_PATH = ROOT / "panels" / "supersplat-logo.svg"
LOGO_TEXTURE_PATH = ROOT / "panels" / "supersplat-logo.tga"
GITHUB_LOGO_PATH = ROOT / "panels" / "github-mark.svg"
GITHUB_TEXTURE_PATH = ROOT / "panels" / "github-mark.tga"


class PanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ET.parse(RML_PATH).getroot()
        cls.rml = RML_PATH.read_text(encoding="utf-8")
        cls.panel = PANEL_PATH.read_text(encoding="utf-8")

    def test_rml_is_well_formed_and_links_sibling_stylesheet(self) -> None:
        self.assertEqual(self.root.tag, "rml")
        body = self.root.find("body")
        self.assertEqual(body.attrib.get("data-model"), "supersplat")
        link = self.root.find("./head/link")
        self.assertIsNotNone(link)
        self.assertEqual(link.attrib["href"], "main_panel.rcss")
        self.assertTrue(RCSS_PATH.is_file())

    def test_form_progress_and_success_are_conditional_peer_views(self) -> None:
        body = self.root.find("body")
        self.assertIsNotNone(body)
        conditions = [
            child.attrib.get("data-if")
            for child in body
            if child.attrib.get("data-if") is not None
        ]
        self.assertEqual(conditions, ["show_form", "show_progress", "show_success"])

    def test_bound_controls_and_actions_have_model_handlers(self) -> None:
        for binding in (
            "scope",
            "source_format",
            "sh_degree",
            "concurrency",
            "title",
            "description",
            "api_key_entry",
        ):
            self.assertIn(f'data-value="{binding}"', self.rml)
            self.assertIn(f'("{binding}",', self.panel)

        for event in (
            "connect",
            "open_api_key_help",
            "open_plugin_github",
            "toggle_advanced",
            "start_upload",
            "cancel_upload",
            "open_editor",
            "upload_another",
        ):
            self.assertIn(f'data-event-click="{event}"', self.rml)
            self.assertIn(f'("{event}",', self.panel)

    def test_upload_requires_valid_scene_and_credentials(self) -> None:
        upload = self.root.find(".//*[@data-event-click='start_upload']")
        self.assertIsNotNone(upload)
        self.assertEqual(upload.text, "Upload to SuperSplat")
        self.assertEqual(upload.attrib.get("data-attrif-disabled"), "!can_upload")
        self.assertIn(
            "return state.token_configured and self._selection_valid and not state.active",
            self.panel,
        )

    def test_errors_have_non_overlapping_placements(self) -> None:
        self.assertEqual(self.rml.count('data-if="show_credentials_error"'), 1)
        self.assertEqual(self.rml.count('data-if="show_upload_error"'), 1)
        self.assertNotIn('data-if="show_form_error"', self.rml)

    def test_api_key_input_has_a_visible_panel_specific_style(self) -> None:
        api_key = self.root.find(".//input[@data-value='api_key_entry']")
        self.assertIsNotNone(api_key)
        self.assertIn("ss-api-key", api_key.attrib.get("class", "").split())

        heading = self.root.find(".//div[@class='ss-credential-field-heading']")
        self.assertIsNotNone(heading)
        self.assertEqual(
            heading.find("./span[@class='ss-credential-label']").text,
            "API key",
        )

        rcss = RCSS_PATH.read_text(encoding="utf-8")
        self.assertIn(".ss-api-key {", rcss)
        self.assertIn("width: auto;", rcss)
        self.assertIn(".ss-api-key:focus {", rcss)

    def test_api_key_help_opens_the_integration_guide(self) -> None:
        help_button = self.root.find(".//*[@data-event-click='open_api_key_help']")
        self.assertIsNotNone(help_button)
        self.assertEqual(help_button.text, "How to add an API key")
        self.assertIn(
            'API_KEY_HELP_URL = "https://developer.playcanvas.com/user-manual/supersplat/integrations/lichtfeld-studio/#connect-to-supersplat"',
            self.panel,
        )
        self.assertIn("get_controller().open_url(API_KEY_HELP_URL)", self.panel)

    def test_remember_checkbox_uses_actual_state_and_ignores_duplicate_events(self) -> None:
        self.assertIn("Store securely with your operating system", self.rml)
        self.assertIn("Your key is never included in upload manifests.", self.rml)
        self.assertNotIn("Remember securely", self.rml)
        self.assertNotIn(
            "self.remember_token = not self.remember_token",
            self.panel,
        )
        self.assertIn("event.current_target()", self.panel)
        self.assertIn('checkbox.has_attribute("checked")', self.panel)
        self.assertIn("if enabled == self.remember_token:", self.panel)
        self.assertIn('self._dirty_model("account_source")', self.panel)

    def test_connect_requires_a_nonempty_api_key(self) -> None:
        connect = self.root.find(".//*[@data-event-click='connect']")
        self.assertIsNotNone(connect)
        self.assertEqual(connect.attrib.get("data-attrif-disabled"), "!can_connect")
        self.assertIn('model.bind_func("can_connect", self._can_connect)', self.panel)
        self.assertIn('self._dirty_model("can_connect")', self.panel)
        self.assertIn("if not self._can_connect():", self.panel)

    def test_content_scope_offers_all_then_visible(self) -> None:
        scope = self.root.find(".//select[@data-value='scope']")
        self.assertIsNotNone(scope)
        self.assertEqual(
            [(option.attrib["value"], option.text) for option in scope],
            [("all", "All splats"), ("visible", "Visible splats")],
        )
        self.assertIn('SCOPES = ("all", "visible")', self.panel)
        self.assertNotIn('"selected"', self.panel)

    def test_persistent_topbar_uses_a_supported_logo_texture(self) -> None:
        topbar = self.root.find("./body/div[@class='ss-topbar']")
        self.assertIsNotNone(topbar)
        self.assertNotIn("data-if", topbar.attrib)

        brand = topbar.find("./div[@class='ss-topbar-brand']")
        self.assertIsNotNone(brand)
        logo = brand.find("./img[@class='ss-topbar-logo']")
        self.assertIsNotNone(logo)
        self.assertEqual(logo.attrib.get("src"), "supersplat-logo.tga")
        self.assertEqual(logo.attrib.get("alt"), "SuperSplat")
        self.assertEqual(
            brand.find("./div/span[@class='ss-topbar-title']").text,
            "SuperSplat",
        )
        self.assertEqual(
            brand.find("./div/span[@class='ss-topbar-subtitle']").text,
            "by PlayCanvas",
        )

        self.assertTrue(LOGO_PATH.is_file())
        svg = ET.parse(LOGO_PATH).getroot()
        self.assertEqual(svg.attrib.get("viewBox"), "64 64 384 384")

        # LichtFeld's RmlUI texture loader accepts uncompressed 24/32-bit TGA,
        # not SVG. Keep the official SVG as the source and ship this RGBA render.
        tga = LOGO_TEXTURE_PATH.read_bytes()
        self.assertEqual(tga[2], 2)  # Uncompressed true-color image.
        self.assertEqual(int.from_bytes(tga[12:14], "little"), 128)
        self.assertEqual(int.from_bytes(tga[14:16], "little"), 128)
        self.assertEqual(tga[16], 32)

        github_button = topbar.find("./button[@data-event-click='open_plugin_github']")
        self.assertIsNotNone(github_button)
        github_icon = github_button.find("./img[@class='ss-github-icon']")
        self.assertEqual(github_icon.attrib.get("src"), "github-mark.tga")
        self.assertEqual(github_icon.attrib.get("alt"), "GitHub")

        self.assertTrue(GITHUB_LOGO_PATH.is_file())
        github_svg = ET.parse(GITHUB_LOGO_PATH).getroot()
        self.assertEqual(github_svg.attrib.get("viewBox"), "0 0 24 24")
        github_tga = GITHUB_TEXTURE_PATH.read_bytes()
        self.assertEqual(github_tga[2], 2)
        self.assertEqual(int.from_bytes(github_tga[12:14], "little"), 64)
        self.assertEqual(int.from_bytes(github_tga[14:16], "little"), 64)
        self.assertEqual(github_tga[16], 32)

        self.assertIn(
            'PLUGIN_GITHUB_URL = "https://github.com/playcanvas/supersplat-lichtfeld-plugin"',
            self.panel,
        )
        self.assertIn("get_controller().open_url(PLUGIN_GITHUB_URL)", self.panel)

    def test_success_actions_have_primary_secondary_and_grouped_tertiary_levels(self) -> None:
        actions = self.root.find(
            ".//div[@data-if='show_success']/div[@class='ss-state-actions']"
        )
        self.assertIsNotNone(actions)

        primary = actions.find("./button[@data-event-click='open_editor']")
        secondary = actions.find("./button[@data-event-click='open_viewer']")
        tertiary = actions.find("./div[@class='ss-tertiary-actions']")
        self.assertIn("btn--primary", primary.attrib.get("class", "").split())
        self.assertIn("btn--secondary", secondary.attrib.get("class", "").split())
        self.assertEqual(
            [button.attrib.get("data-event-click") for button in tertiary],
            ["copy_link", "upload_another"],
        )

    def test_progress_and_success_views_do_not_repeat_state_labels(self) -> None:
        progress = self.root.find(".//div[@data-if='show_progress']")
        success = self.root.find(".//div[@data-if='show_success']")
        self.assertIsNotNone(progress)
        self.assertIsNotNone(success)

        self.assertIsNone(progress.find("./span[@class='ss-eyebrow']"))
        self.assertEqual(
            progress.find("./span[@class='ss-state-heading']").text,
            "{{progress_heading}}",
        )
        self.assertIsNone(success.find("./span[@class='ss-eyebrow']"))
        self.assertEqual(
            success.find("./span[@class='ss-state-heading']").text,
            "Upload complete",
        )
        self.assertEqual(
            success.find("./span[@class='ss-muted ss-state-copy']").text,
            "Your scene is now processing in SuperSplat.",
        )

    def test_scene_summary_uses_a_small_decorative_status_dot(self) -> None:
        rcss = RCSS_PATH.read_text(encoding="utf-8")
        summary_dot = rcss.split(".ss-summary-icon {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 8dp;", summary_dot)
        self.assertIn("height: 8dp;", summary_dot)
        self.assertIn("border-radius: 4dp;", summary_dot)

        summary = self.root.find(".//div[@class='ss-summary']")
        summary_copy = summary.find("./div[@class='ss-summary-copy ss-grow']")
        self.assertIsNotNone(summary_copy)
        self.assertEqual(
            [item.attrib.get("class") for item in summary_copy],
            [
                "ss-summary-title",
                "ss-muted ss-summary-meta",
                "ss-muted ss-summary-meta",
            ],
        )
        self.assertEqual(summary_copy[1].attrib.get("data-if"), "selection_valid")

    def test_form_controls_keep_the_host_native_vertical_metrics(self) -> None:
        rcss = RCSS_PATH.read_text(encoding="utf-8")
        api_key_rule = rcss.split(".ss-api-key {", 1)[1].split("}", 1)[0]
        self.assertNotIn("height:", api_key_rule)
        self.assertNotIn("ss-control", self.rml)
        self.assertNotIn(".ss-control", rcss)

    def test_credentials_do_not_add_spacing_below_the_persistent_topbar(self) -> None:
        rcss = RCSS_PATH.read_text(encoding="utf-8")
        self.assertNotIn(".ss-credentials {", rcss)


if __name__ == "__main__":
    unittest.main()
