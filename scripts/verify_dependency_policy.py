from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.*)?$"
)
GO_REQUIREMENT = re.compile(r"^\s*([\w./-]+)\s+(v[^\s]+)(?:\s+//.*)?$")


def main() -> int:
    errors: list[str] = []
    python_dependencies = 0
    for path in sorted(ROOT.glob("services/*/requirements*.txt")):
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            python_dependencies += 1
            if not PINNED_REQUIREMENT.fullmatch(line):
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: dependency must use == pin: {line}")

    package_json = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    if package_lock.get("lockfileVersion", 0) < 3:
        errors.append("apps/web/package-lock.json must use lockfileVersion >= 3")
    lock_root = package_lock.get("packages", {}).get("", {})
    node_dependencies = 0
    for group in ("dependencies", "devDependencies"):
        declared = package_json.get(group, {})
        locked_declared = lock_root.get(group, {})
        if declared != locked_declared:
            errors.append(f"apps/web package.json and package-lock.json disagree for {group}")
        for name in declared:
            node_dependencies += 1
            record = package_lock.get("packages", {}).get(f"node_modules/{name}")
            if not record or not record.get("version"):
                errors.append(f"apps/web package lock has no resolved version for {name}")
            elif not record.get("integrity"):
                errors.append(f"apps/web package lock has no integrity hash for {name}")

    go_mod = (ROOT / "services/ontology/go.mod").read_text(encoding="utf-8")
    go_dependencies = 0
    in_block = False
    for raw in go_mod.splitlines():
        line = raw.strip()
        if line == "require (":
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        candidate = line.removeprefix("require ") if line.startswith("require ") else line if in_block else ""
        if not candidate:
            continue
        match = GO_REQUIREMENT.fullmatch(candidate)
        if not match:
            errors.append(f"services/ontology/go.mod has an unversioned requirement: {candidate}")
        else:
            go_dependencies += 1

    for dockerfile in ROOT.rglob("Dockerfile"):
        for line_number, raw in enumerate(dockerfile.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.upper().startswith("FROM "):
                continue
            image = raw.split()[1]
            if image.lower().endswith(":latest") or (":" not in image and "@" not in image):
                errors.append(
                    f"{dockerfile.relative_to(ROOT)}:{line_number}: base image must use a non-latest tag or digest"
                )

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for image in re.findall(r"(?m)^\s*image:\s*([^\s#]+)", compose):
        if image.lower().endswith(":latest") or (":" not in image and "@" not in image):
            errors.append(f"docker-compose.yml image is not pinned: {image}")

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    for required in (
        "weknora",
        "tdesign",
        "sqlglot",
        "langgraph",
        "opentelemetry",
        "pgx",
        "wren core python",
        "jackson",
        "cython",
    ):
        if required not in notices:
            errors.append(f"THIRD_PARTY_NOTICES.md is missing required provenance entry: {required}")

    forbidden_roots = (
        ROOT / "third_party/openbkn",
        ROOT / "third_party/weknora",
        ROOT / "vendor/openbkn",
        ROOT / "vendor/weknora",
    )
    for path in forbidden_roots:
        if path.exists():
            errors.append(f"unreviewed upstream source tree is forbidden: {path.relative_to(ROOT)}")

    for workflow in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        for line_number, raw in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            match = re.search(r"\buses:\s*[^\s@]+@([^\s#]+)", raw)
            if match and not re.fullmatch(r"[0-9a-f]{40}", match.group(1)):
                errors.append(
                    f"{workflow.relative_to(ROOT)}:{line_number}: action must be pinned to a full commit SHA"
                )

    if errors:
        print("Dependency policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "Dependency policy passed: "
        f"{python_dependencies} pinned Python, {node_dependencies} locked Node, "
        f"{go_dependencies} versioned Go dependencies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
