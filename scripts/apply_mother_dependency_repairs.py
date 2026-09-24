#!/usr/bin/env python3
"""Apply conservative package.json dependency repairs from a screening manifest."""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

RULES = {
    "lucide-react": "^0.294.0",
    "react-router-dom": "^6.20.0",
    "zustand": "^4.5.5",
    "pinia": "^2.1.7",
    "tailwindcss": "^3.3.6",
    "prop-types": "^15.8.1",
    "date-fns": "^3.6.0",
}


def main(manifest_path: str, output_path: str):
    manifest = json.loads(Path(manifest_path).read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    for item in manifest["items"]:
        root = Path(item["path"])
        package_path = root / "package.json"
        record = {"path": str(root), "requested": item.get("missing_packages", []), "status": "skipped"}
        try:
            package = json.loads(package_path.read_text())
            if not isinstance(package, dict):
                raise ValueError("package.json is not an object")
            deps = package.setdefault("dependencies", {})
            missing = [name for name in item.get("missing_packages", []) if name not in deps and name in RULES]
            if missing:
                backup = package_path.with_name("package.json.before-rule-repair")
                if not backup.exists():
                    shutil.copy2(package_path, backup)
                for name in missing:
                    deps[name] = RULES[name]
                package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
                record.update({"status": "repaired", "added": {name: RULES[name] for name in missing}, "backup": str(backup)})
            else:
                record["status"] = "already_fixed_or_no_rule"
        except Exception as exc:
            record.update({"status": "error", "error": repr(exc)})
        results.append(record)
    out = {"manifest": manifest_path, "rule_versions": RULES, "timestamp": stamp, "results": results}
    Path(output_path).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    from collections import Counter
    print(json.dumps(Counter(r["status"] for r in results), ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: apply_mother_dependency_repairs.py MANIFEST OUTPUT")
    main(sys.argv[1], sys.argv[2])
