"""Print one platform from rules.yaml. Usage: python spotcheck.py <prefix>"""
import sys
import yaml
from pathlib import Path

prefix = sys.argv[1] if len(sys.argv) > 1 else "Intel 6X-7X"
data = yaml.safe_load(Path("private/rules.yaml").read_text(encoding="utf-8"))
match = {k: v for k, v in data["platforms"].items() if k.startswith(prefix)}
if not match:
    available = list(data["platforms"].keys())
    sys.exit(f"No platform starts with {prefix!r}. Available: {available}")
print(yaml.safe_dump(match, allow_unicode=True, sort_keys=False, indent=2))
