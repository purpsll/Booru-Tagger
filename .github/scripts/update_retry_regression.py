from pathlib import Path

path = Path("plugins/DanbooruTagImporter/tests/test_plugin_safety.py")
text = path.read_text(encoding="utf-8")
old = '''        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \\
             mock.patch.object(plugin, "ENABLE_RULE34", False), \\
             mock.patch.object(plugin, "ENABLE_E621", False), \\
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \\
             mock.patch.object(plugin, "ENABLE_E621_IQDB", False), \\
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \\
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \\
             mock.patch.object(plugin, "danbooru_post", side_effect=rate_limited):
'''
new = '''        with mock.patch.object(plugin, "ENABLE_GELBOORU", False), \\
             mock.patch.object(plugin, "ENABLE_RULE34", False), \\
             mock.patch.object(plugin, "ENABLE_E621", True), \\
             mock.patch.object(plugin, "ENABLE_DANBOORU_IQDB", False), \\
             mock.patch.object(plugin, "ENABLE_E621_IQDB", True), \\
             mock.patch.object(plugin, "ENABLE_SAUCENAO", False), \\
             mock.patch.object(plugin, "ENABLE_LOCAL_PHASH_REUSE", False), \\
             mock.patch.object(plugin, "e621_iqdb", side_effect=rate_limited):
'''
if old not in text:
    raise SystemExit("obsolete retry regression block not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
