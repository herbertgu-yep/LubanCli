"""Incremental Python symbol and import-reference index for one worktree."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path


def symbols(source: bytes, path: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source, filename=path)
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    module = ".".join(parts).removesuffix(".__init__")
    package = module if parts[-1] == "__init__" else module.rpartition(".")[0]
    rows = []
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = (
                package.split(".")[: len(package.split(".")) - node.level + 1]
                if node.level
                else []
            )
            prefix = ".".join(filter(None, [*base, node.module]))
            for item in node.names:
                aliases[item.asname or item.name] = ".".join(
                    filter(None, [prefix, item.name])
                )
                rows.append(("import", aliases[item.asname or item.name], node.lineno))
        elif isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = (
                    item.name if item.asname else item.name.split(".")[0]
                )

    class Visitor(ast.NodeVisitor):
        scope = module

        def definition(self, node):
            previous = self.scope
            self.scope += "." + node.name
            rows.append(("def", self.scope, node.lineno))
            self.generic_visit(node)
            self.scope = previous

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = definition

        def visit_Call(self, node):
            target = node.func
            names = []
            while isinstance(target, ast.Attribute):
                names.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                head = aliases.get(target.id, module + "." + target.id)
                rows.append(("call", ".".join([head, *reversed(names)]), node.lineno))
            self.generic_visit(node)

    Visitor().visit(tree)
    return rows


class RepositoryMap:
    def __init__(self, box):
        self.box = box
        self.db_path = box.s.home / ("symbols-" + box.s.wid + ".sqlite3")

    def query(self, query: str, limit: int = 32) -> list[str]:
        terms = set(re.findall(r"[\w.]+", query.casefold()))
        result = []
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS idx(path PRIMARY KEY,mtime,size,records)"
                )
                cached = {
                    r[0]: r[1:]
                    for r in db.execute("SELECT path,mtime,size,records FROM idx")
                }
                for file in self.box.files():
                    if file.suffix != ".py":
                        continue
                    rel = file.relative_to(self.box.s.root).as_posix()
                    prev = cached.pop(rel, None)
                    try:
                        st = file.stat()
                        if st.st_size > 1024 * 1024:
                            db.execute("DELETE FROM idx WHERE path=?", (rel,))
                            continue
                        if prev and prev[:2] == (st.st_mtime_ns, st.st_size):
                            rows = json.loads(prev[2])
                        else:
                            try:
                                rows = symbols(file.read_bytes(), rel)
                            except (SyntaxError, ValueError, RecursionError):
                                rows = []
                            db.execute(
                                "INSERT OR REPLACE INTO idx VALUES(?,?,?,?)",
                                (rel, st.st_mtime_ns, st.st_size, json.dumps(rows)),
                            )
                    except OSError:
                        db.execute("DELETE FROM idx WHERE path=?", (rel,))
                        continue
                    for kind, name, line in rows:
                        words = {
                            name.casefold(),
                            *name.casefold().split("."),
                            *rel.casefold().split("/"),
                        }
                        score = len(terms & words)
                        if score or (not query and kind == "def"):
                            result.append((-score, rel, line, kind, name))
                db.executemany("DELETE FROM idx WHERE path=?", [(p,) for p in cached])
        finally:
            db.close()
        return [
            f"{p}:{n}  {kind} {name}" for _, p, n, kind, name in sorted(result)[:limit]
        ]
