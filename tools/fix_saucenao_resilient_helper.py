from pathlib import Path

path = Path(__file__).with_name("apply_saucenao_resilient_3268.py")
text = path.read_text(encoding="utf-8")
old = '''text = replace_once(\n    text,\n    '_SAUCENAO_OUTAGE_UNTIL = 0.0',\n    '_SAUCENAO_OUTAGE_HITS = 0\\n_SAUCENAO_OUTAGE_STREAK = 0',\n    "SauceNAO outage state",\n)'''
new = '''text = replace_once(\n    text,\n    '_SAUCENAO_QUOTA_ANNOUNCED = False\\n_SAUCENAO_OUTAGE_UNTIL = 0.0',\n    '_SAUCENAO_QUOTA_ANNOUNCED = False\\n_SAUCENAO_OUTAGE_HITS = 0\\n_SAUCENAO_OUTAGE_STREAK = 0',\n    "SauceNAO outage state",\n)'''
if text.count(old) != 1:
    raise SystemExit(f"helper anchor mismatch: {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Fixed SauceNAO helper state anchor")
