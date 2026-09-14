from pathlib import Path

root = Path(__file__).resolve().parents[1]
plugin = root / "plugins" / "DanbooruTagImporter"
perf = plugin / "tests" / "test_deep_performance.py"
task = plugin / "tests" / "test_task_menu.py"

text = perf.read_text(encoding="utf-8")
start = text.index("    def test_saucenao_cooldown_is_quiet_after_initial_outage_warning(self):")
end = text.index("\n\nif __name__ == \"__main__\":", start)
replacement = '''    def test_saucenao_521_is_image_local_and_retry_later_is_info(self):\n        stash = FakeStash()\n        settings = {"saucenao_api_key": "sauce"}\n        outage = urllib.error.HTTPError(\n            "https://saucenao.com", 521, "down", {}, None\n        )\n        with mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \\\n             mock.patch.object(plugin, "e621_iqdb", return_value=None), \\\n             mock.patch.object(plugin, "saucenao_resolve", side_effect=[outage, None]) as sauce, \\\n             mock.patch.object(plugin, "log") as logger:\n            first = plugin.process_image(\n                stash, unresolved_image(), settings, {}, True, {}, {}, {}, {},\n                plugin.PHashIndex(), lookup_mode="deep",\n            )\n            second = plugin.process_image(\n                stash, unresolved_image(), settings, {}, True, {}, {}, {}, {},\n                plugin.PHashIndex(), lookup_mode="deep",\n            )\n\n        self.assertEqual(first, "retry_later")\n        self.assertEqual(second, "no_match")\n        self.assertEqual(sauce.call_count, 2)\n        retry_logs = [\n            call for call in logger.call_args_list if "RETRY LATER" in str(call)\n        ]\n        self.assertEqual(len(retry_logs), 1)\n        self.assertEqual(retry_logs[0].args[0], "INFO")\n'''
perf.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

text = task.read_text(encoding="utf-8")
old = 'self.assertEqual(yaml_version, "3.26.7")'
if text.count(old) != 1:
    raise SystemExit(f"task-menu version anchor mismatch: {text.count(old)}")
task.write_text(text.replace(old, 'self.assertEqual(yaml_version, "3.26.8")', 1), encoding="utf-8")

print("Updated SauceNAO regression expectations for 3.26.8")
