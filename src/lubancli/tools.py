"""Typed development tools. Commands run on the host under explicit approval."""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

from filelock import FileLock
from pathspec import GitIgnoreSpec
from pydantic import BaseModel, ConfigDict, Field

from .checkpoints import capture
from .patch import apply_hunks
from .repository import RepositoryMap
from .sandbox import isolated
from .state import Store, digest, git, git_environment

Notify = Callable[[str, str], Awaitable[None]]
Approve = Callable[[str, str], Awaitable[str]]
SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Directory(Args):
    path: str = "."


class Match(Args):
    pattern: str


class Search(Match):
    text: str = Field(min_length=1)
    pattern: str = "*"


class Read(Directory):
    start: int = Field(default=1, ge=1)
    count: int = Field(default=100, ge=1, le=300)


class Symbols(Args):
    query: str = ""


class Create(Directory):
    content: str


class Edit(Directory):
    old: str
    new: str
    version: str


class Patch(Directory):
    version: str
    patch: str


class Command(Args):
    command: str = Field(min_length=1)
    timeout: int = Field(default=60, ge=1, le=600)


REGISTRY: dict[str, tuple[type[Args], str, str]] = {
    "repo_map": (
        Symbols,
        "read",
        "Find Python definitions, imported symbols and call sites by symbol or qualified name. Empty query lists definitions.",
    ),
    "list_dir": (Directory, "read", "List immediate workspace entries."),
    "match_files": (Match, "read", "Match relative paths using glob patterns."),
    "search": (
        Search,
        "read",
        "Search literal text, returning paths and line numbers.",
    ),
    "read_file": (
        Read,
        "read",
        "Read numbered lines and get the current SHA256 version.",
    ),
    "create_file": (
        Create,
        "edit",
        "Create a new UTF-8 file. Existing files are protected.",
    ),
    "apply_patch": (
        Patch,
        "edit",
        "Apply an exact unified diff to one version-checked UTF-8 file. Supply path, SHA256 version and hunks.",
    ),
    "edit_file": (
        Edit,
        "edit",
        "Replace exactly one literal occurrence after checking the observed version.",
    ),
    "command": (
        Command,
        "command",
        "Run the exact approved host shell command in the launch directory.",
    ),
    "git_status": (Args, "read", "Show worktree status."),
    "git_diff": (Args, "read", "Show staged and unstaged Git changes."),
}


def schema(name: str, description: str, params: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


def bounded(text: str, limit: int = 5000) -> str:
    if len(text) <= limit:
        return text
    errors = [
        line[:300]
        for line in text.splitlines()
        if any(word in line.lower() for word in ("error", "failed", "exception"))
    ][:4]
    return (
        text[: limit // 3]
        + "\n…\n"
        + "\n".join(errors)[: max(0, limit // 3 - 8)]
        + "\n…\n"
        + text[-(limit // 3) :]
    )


class Toolbox:
    def __init__(self, store: Store, sid: str, approve: Approve, notify: Notify):
        self.s = store
        self.sid = sid
        self.approve = approve
        self.notify = notify
        self.command_changes: list[str] = []

    def path(self, path: str) -> Path:
        p = (self.s.start / path).resolve()
        if not p.is_relative_to(self.s.root) or any(
            part.casefold() == ".git" for part in p.relative_to(self.s.root).parts
        ):
            raise ValueError("路径超出工作区或指向 Git 元数据")
        return p

    def files(self, root: Path | None = None) -> Iterator[Path]:
        def walk(directory: Path, rules: list) -> Iterator[Path]:
            ignore = directory / ".gitignore"
            if ignore.is_file() and not ignore.is_symlink():
                rules = [
                    *rules,
                    (
                        directory,
                        GitIgnoreSpec.from_lines(
                            ignore.read_text(errors="replace").splitlines()
                        ),
                    ),
                ]
            for entry in sorted(directory.iterdir()):
                if entry.name in SKIP or entry.is_symlink():
                    continue
                ignored = False
                for parent, spec in rules:
                    match = spec.check_file(
                        entry.relative_to(parent).as_posix()
                        + ("/" if entry.is_dir() else "")
                    )
                    if match.include is not None:
                        ignored = match.include
                if ignored:
                    continue
                if entry.is_dir():
                    yield from walk(entry, rules)
                elif entry.is_file():
                    yield entry

        yield from walk(root or self.s.root, [])

    async def permission(
        self, scope: str, detail: str, change: Create | Edit | None = None
    ):
        key = "grant:" + scope + ":" + detail
        policy = self.s.get(self.sid, "policy", "ask")
        if policy == "readonly" and scope != "read":
            raise PermissionError("当前会话只读")
        if scope == "command" and re.search(
            r"(?:^|[\s;&|])(?:sudo|mkfs(?:\.\w+)?|shutdown|reboot)(?:\s|$)|(?:^|[\s;&|])rm\s+(?:-[\w-]*[rR]|--recursive)|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f)",
            detail,
        ):
            raise PermissionError("命令命中破坏性操作拦截规则")
        if policy == "auto" and scope == "edit" and change is not None:
            return
        if self.s.get(self.sid, key) == "yes":
            return
        if change:
            old, new = (
                (change.old, change.new)
                if isinstance(change, Edit)
                else ("", change.content)
            )
            fragment = "\n".join(
                difflib.unified_diff(
                    old.splitlines(),
                    new.splitlines(),
                    fromfile="替换前",
                    tofile="写入后",
                    lineterm="",
                )
            )
            await self.notify(
                "edit_preview",
                self.s.clean(fragment[:5000])
                + ("\n… 预览已截短" if len(fragment) > 5000 else ""),
            )
        decision = await self.approve(scope, detail)
        if decision == "session" and detail != self.s.clean(detail):
            return
        if decision == "session":
            self.s.put(self.sid, key, "yes")
        elif decision != "once":
            raise PermissionError(f"未授权 {scope} {detail}")

    async def call(
        self,
        name: str,
        raw: dict,
        readonly: bool = False,
        output_limit: int = 2000000,
        journal: tuple[str, str] | None = None,
    ) -> str:
        model, scope, _ = REGISTRY[name]
        args = model.model_validate(raw)
        if scope == "read" and self.s.get(self.sid, "read") == "ask":
            await self.permission(
                "read", name + " " + json.dumps(raw, ensure_ascii=False)
            )
        if readonly and scope != "read":
            raise PermissionError("当前角色只读")
        if isinstance(args, Symbols):
            return (
                "\n".join(
                    await asyncio.to_thread(RepositoryMap(self).query, args.query)
                )
                or "没有匹配符号"
            )
        if isinstance(args, Command):
            await self.permission(scope, args.command)
            return await self.shell(args, output_limit)
        if isinstance(args, Patch):
            args = self.patch_edit(args)
        if isinstance(args, (Edit, Create)):
            p = self.path(args.path)
            await self.permission(scope, str(p), args)
            return self.write(p, args, journal)
        return await asyncio.to_thread(self.read_tool, name, args)

    def read_tool(self, name: str, args: Args) -> str:
        if isinstance(args, Read):
            p = self.path(args.path)
            if not p.is_file() or p.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("只支持读取 64 MiB 以内的普通文件")
            import hashlib

            version = hashlib.sha256()
            lines = []
            with p.open("rb") as f:
                for i, line in enumerate(f, 1):
                    version.update(line)
                    if args.start <= i < args.start + args.count:
                        lines.append(
                            f"{p.relative_to(self.s.root)}:{i} {bounded(line.decode(errors='replace').rstrip(), 1800)}"
                        )
            return f"version={version.hexdigest()}\n" + "\n".join(lines)
        if isinstance(args, Directory):
            return "\n".join(
                str(x.relative_to(self.s.root))
                for x in sorted(self.path(args.path).iterdir())[:500]
                if x.name not in SKIP
            )
        if isinstance(args, Match):
            results = []
            for p in self.files():
                rel = p.relative_to(self.s.root).as_posix()
                if not fnmatch.fnmatch(rel, args.pattern):
                    continue
                if isinstance(args, Search):
                    if p.stat().st_size > 4000000:
                        continue
                    with p.open(errors="replace") as f:
                        for i, hit in enumerate(f, 1):
                            if args.text in hit:
                                results.append(
                                    f"{rel}:{i} {bounded(hit.rstrip(), 500)}"
                                )
                                if len(results) >= 120:
                                    break
                else:
                    results.append(rel)
                if len(results) >= 120:
                    break
            return "\n".join(results)
        if name == "git_status":
            return git(self.s.root, "status", "--short")
        return self.diff()

    def diff(self) -> str:
        parts = [
            git(self.s.root, "diff", "--no-ext-diff", "--no-textconv", *flags)
            for flags in ([], ["--cached"])
        ]
        changed = git(self.s.root, "ls-files", "--others", "--exclude-standard")
        return "\n".join(
            x for x in [*parts, ("新增文件\n" + changed if changed else "")] if x
        )

    def patch_edit(self, patch: Patch) -> Edit:
        data = self.path(patch.path).read_bytes()
        if digest(data) != patch.version:
            raise ValueError("文件版本已变化")
        source = data.decode()
        return Edit(
            path=patch.path,
            version=patch.version,
            old=source,
            new=apply_hunks(source, patch.patch),
        )

    def write(
        self, p: Path, args: Create | Edit, journal: tuple[str, str] | None = None
    ) -> str:
        with FileLock(str(self.s.home / (self.s.wid + ".lock")), timeout=3):
            if self.path(args.path) != p:
                raise ValueError("路径已变化")
            before = ""
            if isinstance(args, Edit):
                data = p.read_bytes()
                before = data.decode()
                if digest(data) != args.version or before.count(args.old) != 1:
                    raise ValueError("编辑冲突，请重新读取并匹配唯一旧文本")
                after = before.replace(args.old, args.new, 1)
            else:
                if p.exists():
                    raise FileExistsError(str(p))
                after = args.content
            ref = capture(
                self.s,
                self.sid,
                p,
                before.encode() if isinstance(args, Edit) else None,
                after.encode(),
                journal,
            )
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(after.encode())
                if isinstance(args, Edit):
                    os.chmod(tmp, p.stat().st_mode & 0o777)
                    os.replace(tmp, p)
                else:
                    os.link(tmp, p)
            finally:
                Path(tmp).unlink(missing_ok=True)
            self.s.db.execute(
                "UPDATE file_checkpoint SET status='applied',after_mode=? WHERE id=?",
                (p.stat().st_mode & 0o777, ref),
            )
            rel = str(p.relative_to(self.s.root))
            return "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    "a/" + rel,
                    "b/" + rel,
                )
            )

    def file_stats(self) -> dict[str, tuple[int, int]]:
        observed = {}
        for path in self.files():
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            observed[str(path.relative_to(self.s.root))] = (st.st_mtime_ns, st.st_size)
        return observed

    async def shell(self, args: Command, limit: int) -> str:
        self.command_changes = []
        before = await asyncio.to_thread(self.file_stats)
        try:
            outcome = await self.run_command(args, limit)
        finally:
            after = await asyncio.to_thread(self.file_stats)
            self.command_changes = sorted(
                p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
            )
        return json.dumps(
            {**outcome, "changed_paths": self.command_changes}, ensure_ascii=False
        )

    async def run_command(self, args: Command, limit: int) -> dict:
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            if self.s.get(self.sid, "sandbox") == "on":
                argv, env = isolated(
                    self.s.root, self.s.start, args.command, Path(directory)
                )
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    env=env,
                    cwd=self.s.start,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    args.command,
                    env=git_environment(),
                    cwd=self.s.start,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                )
            return await self.collect_command(proc, args, limit, started)

    async def collect_command(self, proc, args, limit, started):
        streams: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        size = 0

        async def drain(name, reader):
            nonlocal size
            pending = bytearray()
            while chunk := await reader.read(4096):
                size += len(chunk)
                if size > limit:
                    raise ValueError("命令超过输出预算")
                streams[name].append(chunk)
                pending.extend(chunk)
                boundary = pending.rfind(b"\n") + 1
                if boundary:
                    await self.notify(
                        name, self.s.clean(pending[:boundary].decode(errors="replace"))
                    )
                    del pending[:boundary]
            if pending:
                await self.notify(name, self.s.clean(pending.decode(errors="replace")))

        tasks = [
            asyncio.create_task(drain("stdout", proc.stdout)),
            asyncio.create_task(drain("stderr", proc.stderr)),
        ]
        reason = ""
        try:
            async with asyncio.timeout(args.timeout):
                await asyncio.gather(*tasks)
                await proc.wait()
        except (TimeoutError, ValueError) as exc:
            reason = str(exc) or "执行超时"
        finally:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.returncode is None:
                proc.kill()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await proc.wait()
        result = {k: b"".join(v).decode(errors="replace") for k, v in streams.items()}
        return {
            **result,
            "cwd": str(self.s.start),
            "command": args.command,
            "exit_code": proc.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "cancelled": bool(reason),
            "reason": reason,
        }
