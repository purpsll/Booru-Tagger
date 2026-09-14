from pathlib import Path

path = Path("plugins/DanbooruTagImporter/tests/test_plugin_safety.py")
text = path.read_text(encoding="utf-8")
start_marker = "    def test_deep_retry_keeps_unresolved_status(self):\n"
end_marker = "    def test_forced_no_match_does_not_downgrade_imported(self):\n"
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit("deep retry regression function boundaries not found")
block = text[start:end]
replacements = {
    'mock.patch.object(plugin, "ENABLE_E621", False)':
        'mock.patch.object(plugin, "ENABLE_E621", True)',
    'mock.patch.object(plugin, "ENABLE_E621_IQDB", False)':
        'mock.patch.object(plugin, "ENABLE_E621_IQDB", True)',
    'mock.patch.object(plugin, "danbooru_post", side_effect=rate_limited)':
        'mock.patch.object(plugin, "e621_iqdb", side_effect=rate_limited)',
}
for old, new in replacements.items():
    if old not in block:
        raise SystemExit(f"deep retry regression anchor missing: {old}")
    block = block.replace(old, new, 1)
text = text[:start] + block + text[end:]
path.write_text(text, encoding="utf-8")
