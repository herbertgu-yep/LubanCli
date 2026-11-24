"""Discover explicit project checks without executing manifest contents."""

import json
import re
import tomllib
from pathlib import Path


def discover(root: Path) -> list[str]:
    commands = []
    manifest = root / "pyproject.toml"
    config = {}
    if manifest.is_file() and not manifest.is_symlink():
        config = tomllib.loads(manifest.read_text()).get("tool", {})
    if "ruff" in config or (root / "ruff.toml").is_file():
        commands.append("ruff check .")
    if "mypy" in config or (root / "mypy.ini").is_file():
        commands.append("mypy .")
    if (
        "pytest" in config
        or (root / "pytest.ini").is_file()
        or (root / "tests").is_dir()
        and any((root / "tests").glob("test_*.py"))
    ):
        commands.append("python -m pytest -q")
    if (
        manifest.is_file()
        and not manifest.is_symlink()
        and "build-system" in tomllib.loads(manifest.read_text())
    ):
        commands.append("uv build")
    if (root / "uv.lock").is_file():
        commands = [
            ("uv run --locked " + c) if c != "uv build" else c for c in commands
        ]
    package = root / "package.json"
    if package.is_file() and not package.is_symlink():
        scripts = json.loads(package.read_text()).get("scripts", {})
        for name in ("lint", "typecheck", "test", "build"):
            if name in scripts:
                commands.append("npm run " + name)
    if (root / "Cargo.toml").is_file():
        commands.extend(["cargo check", "cargo test"])
    if (root / "go.mod").is_file():
        commands.extend(["go test ./...", "go build ./..."])
    return commands


def workspace_checks(start: Path, root: Path) -> list[str]:
    """Prefer the nearest project manifest when launched inside a monorepo."""
    directory = start
    while True:
        checks = discover(directory)
        if checks:
            if start == directory:
                return checks
            import os
            import shlex

            prefix = "cd " + shlex.quote(os.path.relpath(directory, start)) + " && "
            return [prefix + command for command in checks]
        if directory == root or root not in directory.parents:
            return []
        directory = directory.parent


def check_fingerprint(outcome: dict) -> str:
    """Compare failure evidence without treating exit codes alone as equivalent."""
    from .state import digest

    if outcome["exit_code"] == 0 or outcome.get("cancelled"):
        return ""
    stdout = outcome["stdout"]
    stderr = outcome["stderr"]
    if not (stdout.strip() or stderr.strip()):
        return ""

    # Pytest's final timing changes between runs even when the failure is unchanged.
    def clean(output):
        return re.sub(
            r" in \d+(?:\.\d+)?s(?= ?(?:=+)?$)", "", output, flags=re.MULTILINE
        )

    return digest(
        json.dumps(
            [outcome["exit_code"], clean(stdout), clean(stderr)], ensure_ascii=False
        ).encode()
    )
