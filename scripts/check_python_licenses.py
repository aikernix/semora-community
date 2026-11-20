from __future__ import annotations

import json
import sys
from pathlib import Path


FORBIDDEN = (
    "AGPL",
    "GNU GENERAL PUBLIC LICENSE",
    "GPL-2",
    "GPL-3",
    "SSPL",
    "BUSL",
    "COMMONS CLAUSE",
    "PROPRIETARY",
    "UNKNOWN",
)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_python_licenses.py <pip-licenses.json>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    records = json.loads(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for record in records:
        license_name = str(record.get("License") or "UNKNOWN").upper()
        strong_copyleft_view = license_name.replace("LGPL", "").replace(
            "GNU LESSER GENERAL PUBLIC LICENSE", ""
        )
        if any(token in strong_copyleft_view for token in FORBIDDEN):
            failures.append(
                f"{record.get('Name')}=={record.get('Version')}: {record.get('License')}"
            )
    if failures:
        print("Forbidden or unknown Python licenses:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Python license policy passed for {len(records)} installed distributions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
