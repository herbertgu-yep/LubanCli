"""Evaluate model-generated changes against isolated, synthetic Git tasks."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .service import Service
from .state import Profile, Store, git_environment

CASES = (
    (
        "config-update",
        {
            "settings.json": '{"retries": 2, "timeout": 15}\n',
            "notes.txt": "Preserve this file.\n",
        },
        "Change retries to 4 in settings.json. Keep the timeout at 15 and preserve notes.txt.",
        {"settings.json": {"retries": 4, "timeout": 15}},
    ),
    (
        "config-create",
        {"defaults.json": '{"region": "local", "limit": 5}\n'},
        "Create overrides.json with region set to local and limit set to 8. Leave defaults.json unchanged.",
        {"overrides.json": {"region": "local", "limit": 8}},
    ),
)


async def evaluate(profile: Profile, limit: int = len(CASES)) -> dict:
    """Exercise the configured model with only case-specific edits approved."""
    results = []
    for name, original, prompt, expected in CASES[:limit]:
        with tempfile.TemporaryDirectory(prefix="luban-eval-") as directory:
            root = Path(directory) / "project"
            root.mkdir()
            for path, contents in original.items():
                (root / path).write_text(contents, encoding="utf-8")
            for argv in (
                ["init", "-q"],
                ["add", "."],
                ["commit", "-qm", "Case baseline"],
            ):
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Luban Evaluation",
                        "-c",
                        "user.email=eval@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "core.hooksPath=/dev/null",
                        *argv,
                    ],
                    cwd=root,
                    env=git_environment(),
                    check=True,
                    capture_output=True,
                )
            root = root.resolve()
            denied: list[str] = []

            async def approve(scope, detail):
                if scope == "edit" and detail in {
                    str(root / item) for item in expected
                }:
                    return "once"
                denied.append(scope)
                return "deny"

            async def notify(kind, text):
                pass

            store = Store(root, Path(directory) / "data")
            failure = ""
            try:
                store.store_profile(profile)
                service = Service(store, approve, notify)
                await service.submit(prompt)
            except Exception as exc:
                failure = type(exc).__name__
            finally:
                store.db.close()
            checks = {}
            for path, wanted in expected.items():
                try:
                    checks[path] = json.loads((root / path).read_text()) == wanted
                except (ValueError, OSError):
                    checks[path] = False
            untouched = all(
                (root / path).is_file()
                and (root / path).read_bytes() == contents.encode()
                for path, contents in original.items()
                if path not in expected
            )
            unexpected = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
                and ".git" not in path.relative_to(root).parts
                and path.relative_to(root).as_posix()
                not in original.keys() | expected.keys()
            )
            results.append(
                {
                    "case": name,
                    "passed": all(checks.values())
                    and untouched
                    and not unexpected
                    and not denied
                    and not failure,
                    "checks": checks,
                    "untouched": untouched,
                    "unexpected_files": unexpected,
                    "denied": denied,
                    "error": failure or None,
                }
            )
    return {
        "passed": sum(case["passed"] for case in results),
        "total": len(results),
        "cases": results,
    }
