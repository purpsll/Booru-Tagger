from pathlib import Path

path = Path(__file__).resolve().parent / "apply_e621_iqdb_pacing_3266.py"
text = path.read_text(encoding="utf-8")

old = '''replace_once(\n    main,\n    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\\n            retries=DEEP_VISUAL_HTTP_RETRIES,\\n',\n    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\\n'\n    '            min_interval=0.0,\\n'\n    '            retries=DEEP_VISUAL_HTTP_RETRIES,\\n',\n)\nreplace_once(\n    main,\n    '        ) as resp:\\n            raw = resp.read().decode("utf-8", errors="replace")\\n    except urllib.error.HTTPError as exc:\\n',\n    '        ) as resp:\\n'\n    '            raw = resp.read().decode("utf-8", errors="replace")\\n'\n    '        _e621_iqdb_note_success()\\n'\n    '    except urllib.error.HTTPError as exc:\\n',\n)\n'''

new = '''replace_once(\n    main,\n    '        with HTTP.urlopen(\\n'\n    '            req,\\n'\n    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\\n'\n    '            retries=DEEP_VISUAL_HTTP_RETRIES,\\n'\n    '        ) as resp:\\n'\n    '            raw = resp.read().decode("utf-8", errors="replace")\\n'\n    '    except urllib.error.HTTPError as exc:\\n',\n    '        with HTTP.urlopen(\\n'\n    '            req,\\n'\n    '            timeout=DEEP_VISUAL_TIMEOUT_SECONDS,\\n'\n    '            min_interval=0.0,\\n'\n    '            retries=DEEP_VISUAL_HTTP_RETRIES,\\n'\n    '        ) as resp:\\n'\n    '            raw = resp.read().decode("utf-8", errors="replace")\\n'\n    '        _e621_iqdb_note_success()\\n'\n    '    except urllib.error.HTTPError as exc:\\n',\n)\n'''

count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one ambiguous helper block, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Narrowed e621 IQDB patch anchors")
