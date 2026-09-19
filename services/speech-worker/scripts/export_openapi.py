"""Export the canonical Worker OpenAPI document for shared type generation."""

from __future__ import annotations

import json
from pathlib import Path

from speech_capture_worker.api import app


def main() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    target = repository_root / "packages" / "protocol" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


if __name__ == "__main__":
    main()
