import pathlib
import re
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
MAIN = PLUGIN_DIR / "StashMetadataMigrator.py"
CLIENT = PLUGIN_DIR / "stash_client.py"
MANIFEST = PLUGIN_DIR / "StashMetadataMigrator.yml"


class PluginSafetyTests(unittest.TestCase):
    def test_plugin_contains_no_media_create_mutations(self):
        text = (MAIN.read_text(encoding="utf-8") + "\n" + CLIENT.read_text(encoding="utf-8"))
        for forbidden in (
            "sceneCreate(",
            "imageCreate(",
            "fileCreate(",
            "sceneAssignFile(",
        ):
            self.assertNotIn(forbidden, text)

    def test_only_update_mutations_are_used_for_media(self):
        text = CLIENT.read_text(encoding="utf-8")
        self.assertIn("sceneUpdate(input:", text)
        self.assertIn("imageUpdate(input:", text)

    def test_manifest_exposes_analyze_before_restore(self):
        text = MANIFEST.read_text(encoding="utf-8")
        names = re.findall(r"^  - name: (.+)$", text.split("tasks:\n", 1)[1], flags=re.MULTILINE)
        self.assertEqual(names, [
            "1. Analyze Old Stash Export (No Changes)",
            "2. Restore Metadata to Existing Media",
        ])

    def test_manifest_version_is_1_0_0(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^version:\s*1\.0\.0$")

    def test_source_path_is_user_configurable(self):
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn("migration_source_path:", text)
        self.assertIn("Old Stash export path", text)


if __name__ == "__main__":
    unittest.main()
