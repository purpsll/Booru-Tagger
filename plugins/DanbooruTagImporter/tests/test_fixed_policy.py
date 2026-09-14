import pathlib
import re
import unittest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
YAML_PATH = PLUGIN_DIR / "DanbooruTagImporter.yml"
SOURCE_PATH = PLUGIN_DIR / "DanbooruTagImporter.py"

EXPECTED_USER_SETTINGS = {
    "danbooru_login",
    "danbooru_api_key",
    "gelbooru_user_id",
    "gelbooru_api_key",
    "rule34_user_id",
    "rule34_api_key",
    "e621_username",
    "e621_api_key",
    "saucenao_api_key",
    "saucenao_requests_per_30_seconds",
}


class FixedPolicyTests(unittest.TestCase):
    def test_yaml_exposes_only_user_specific_fields(self):
        text = YAML_PATH.read_text(encoding="utf-8")
        settings_block = text.split("settings:\n", 1)[1].split("\ntasks:\n", 1)[0]
        keys = set(re.findall(r"^  ([a-z0-9_]+):$", settings_block, flags=re.MULTILINE))
        self.assertEqual(keys, EXPECTED_USER_SETTINGS)

    def test_runtime_reads_only_user_specific_settings_and_saucenao_rate(self):
        text = SOURCE_PATH.read_text(encoding="utf-8")
        keys = set(re.findall(r'settings\.get\("([a-z0-9_]+)"\)', text))
        self.assertEqual(keys, EXPECTED_USER_SETTINGS)

    def test_legacy_tuning_keys_are_not_in_plugin_ui(self):
        text = YAML_PATH.read_text(encoding="utf-8")
        for old_key in (
            "saucenao_accept_review_band",
            "local_phash_max_distance",
            "provider_request_interval_ms",
            "artist_mapping",
            "character_mapping",
            "merge_similar_performers",
            "max_images_per_run",
        ):
            self.assertNotRegex(text, rf"^  {re.escape(old_key)}:$")


if __name__ == "__main__":
    unittest.main()
