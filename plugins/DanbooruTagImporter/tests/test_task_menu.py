import pathlib
import re
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
YAML_PATH = PLUGIN_DIR / "DanbooruTagImporter.yml"
SOURCE_PATH = PLUGIN_DIR / "DanbooruTagImporter.py"
CONSTANTS_PATH = PLUGIN_DIR / "constants.py"

EXPECTED_TASKS = [
    "1. Scan Unprocessed Images (Fast)",
    "2. Preview Deep Match (10 Unresolved, No Changes)",
    "3. Deep Match All Unresolved Images",
    "4. Recheck Review Candidates",
    "5. Retry No-Match Images",
]


class TaskMenuTests(unittest.TestCase):
    def test_task_menu_is_small_ordered_and_user_facing(self):
        text = YAML_PATH.read_text(encoding="utf-8")
        names = re.findall(r"^  - name: (.+)$", text.split("tasks:\n", 1)[1], flags=re.MULTILINE)
        self.assertEqual(names, EXPECTED_TASKS)

    def test_removed_technical_tasks_do_not_appear(self):
        text = YAML_PATH.read_text(encoding="utf-8").casefold()
        for removed in (
            "diagnostics - test configuration",
            "force deep recheck",
            "dry run - fast",
        ):
            self.assertNotIn(removed, text)

    def test_no_public_diagnostics_mode_remains(self):
        source = SOURCE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("def run_diagnostics(", source)
        self.assertNotIn('mode == "diagnostics"', source)

    def test_no_automatic_image_hook_is_published(self):
        text = YAML_PATH.read_text(encoding="utf-8")
        self.assertNotIn("\nhooks:", text)
        self.assertNotIn("Image.Create.Post", text)

    def test_release_versions_match(self):
        yaml_text = YAML_PATH.read_text(encoding="utf-8")
        constants_text = CONSTANTS_PATH.read_text(encoding="utf-8")
        yaml_version = re.search(r"^version:\s*([^\s]+)$", yaml_text, flags=re.MULTILINE).group(1)
        code_version = re.search(r'^VERSION\s*=\s*"([^"]+)"$', constants_text, flags=re.MULTILINE).group(1)
        self.assertEqual(yaml_version, "3.26.13")
        self.assertEqual(code_version, yaml_version)


if __name__ == "__main__":
    unittest.main()
