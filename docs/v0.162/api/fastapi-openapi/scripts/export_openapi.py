# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 MOZGII, Inc. (https://mozgii.com/)
"""Write a FastAPI app's OpenAPI schema to a file, without starting a server.

Usage (from the project root, inside its environment):
    uv run python scripts/export_openapi.py app.main:app openapi.json
    uv run python scripts/export_openapi.py app.main:app openapi.json --check   # exit 1 if the file is stale

Keys are sorted and the output ends with a newline, so the file diffs cleanly in review and CI.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from fastapi import FastAPI


def load_app(target: str) -> FastAPI:
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"expected module:attribute, got {target!r}")
    sys.path.insert(0, str(Path.cwd()))
    app = getattr(importlib.import_module(module_name), attr)
    if not isinstance(app, FastAPI):
        raise SystemExit(f"{target} is not a FastAPI app")
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a FastAPI app's OpenAPI schema to a file.")
    parser.add_argument("app", help="import path of the FastAPI app, e.g. app.main:app")
    parser.add_argument("output", type=Path, help="file to write, e.g. openapi.json")
    parser.add_argument("--check", help="compare instead of writing", action="store_true")
    args = parser.parse_args()

    app = load_app(args.app)
    schema = app.openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != rendered:
            print(f"{args.output} is out of date; run without --check to regenerate", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0

    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    status = main()
    raise SystemExit(status)
