# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 MOZGII, Inc. (https://mozgii.com/)
"""Find blocking calls inside `async def` functions of a FastAPI codebase.

Usage: python3 find_blocking_calls.py app/ [more paths]
Exit code 1 when anything is found, so it can run in CI or a pre-commit hook.
Standard library only; reads files, never imports them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Call targets that block the event loop when awaited code would be expected.
BLOCKING = {
    "time.sleep": "use `await asyncio.sleep()`",
    "requests.get": "use `httpx.AsyncClient`",
    "requests.post": "use `httpx.AsyncClient`",
    "requests.put": "use `httpx.AsyncClient`",
    "requests.patch": "use `httpx.AsyncClient`",
    "requests.delete": "use `httpx.AsyncClient`",
    "requests.request": "use `httpx.AsyncClient`",
    "httpx.get": "use `httpx.AsyncClient`",
    "httpx.post": "use `httpx.AsyncClient`",
    "urllib.request.urlopen": "use `httpx.AsyncClient`",
    "subprocess.run": "use `asyncio.create_subprocess_exec`",
    "subprocess.check_output": "use `asyncio.create_subprocess_exec`",
    "subprocess.call": "use `asyncio.create_subprocess_exec`",
    "os.system": "use `asyncio.create_subprocess_exec`",
    "open": "use `anyio.open_file` or `await asyncio.to_thread(...)` for large files",
    "psycopg2.connect": "use an async driver (asyncpg, psycopg async)",
    "sqlite3.connect": "use aiosqlite or a sync `def` endpoint",
}


def dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class Finder(ast.NodeVisitor):
    def __init__(self, source: Path) -> None:
        self.source = source
        self.depth = 0
        self.found: list[str] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved, self.depth = self.depth, 0      # a nested sync def runs wherever it is called
        self.generic_visit(node)
        self.depth = saved

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        if self.depth and name in BLOCKING:
            self.found.append(f"{self.source}:{node.lineno}: {name}() inside async def; {BLOCKING[name]}")
        self.generic_visit(node)


def scan(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_bytes(), str(path))
    except (SyntaxError, ValueError) as exc:
        return [f"{path}: not parsed ({exc.__class__.__name__})"]
    finder = Finder(path)
    finder.visit(tree)
    return finder.found


def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    roots = [Path(a) for a in argv] or [Path()]
    files = [p for r in roots for p in ([r] if r.is_file() else sorted(r.rglob("*.py")))
             if ".venv" not in p.parts and "migrations" not in p.parts]
    problems = [line for f in files for line in scan(f)]
    print("\n".join(problems) if problems else f"no blocking calls found in {len(files)} files")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
