"""Installed console entry points."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown

from .agent import Event
from .service import Service
from .state import Profile, Store

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
console = Console()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """在当前目录启动 LubanCli。"""
    if ctx.invoked_subcommand is None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            typer.echo('非交互环境请使用 Luban run "任务" --json', err=True)
            raise typer.Exit(2)
        from .ui import LubanApp

        LubanApp(Store(Path.cwd())).run()


@app.command()
def run(
    task: str,
    session: str = "",
    machine: Annotated[bool, typer.Option("--json")] = False,
    allow_edit: list[str] = [],
    allow_command: list[str] = [],
    allow_external: list[str] = [],
):
    """执行需求或斜杠命令。授权按完整路径或完整命令匹配。"""
    s = Store(Path.cwd())
    denied = []

    def emit(kind, text):
        if machine:
            typer.echo(Event(kind=kind, text=text).model_dump_json())
        elif kind == "answer":
            console.print(Markdown(text))
        elif kind not in {"delta"}:
            console.print(text, markup=False, highlight=False)

    async def approve(scope, detail):
        allowed = {
            "edit": {str((s.start / p).resolve()) for p in allow_edit},
            "command": set(allow_command),
            "external": set(allow_external),
        }
        if detail in allowed.get(scope, set()):
            return "once"
        denied.append(f"{scope} {detail}")
        return "deny"

    async def notify(kind, text):
        emit(kind, s.clean(text))

    async def invoke():
        svc = Service(s, approve, notify, session)
        emit("session", svc.sid)
        if session:
            emit("resume", svc.resume(session))
        result = await svc.submit(task)
        emit("answer", result)
        if denied:
            raise PermissionError("未授权：" + "，".join(denied))

    try:
        asyncio.run(invoke())
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except Exception as exc:
        emit("error", s.clean(str(exc)) or type(exc).__name__)
        raise typer.Exit(3 if denied else 1)
    finally:
        s.db.close()


@app.command()
def model_add(
    name: str,
    base_url: str,
    model: str,
    env: str = "",
    window: int = 32768,
    output: int = 2048,
    tools: bool = True,
    tokenizer: str = "",
    calls: int = 40,
    seconds: int = 600,
    output_bytes: int = 2000000,
):
    """测试连接后保存命名配置。可指定密钥环境变量；直接填写 API Key 请使用终端模型设置。"""
    s = Store(Path.cwd())
    try:
        p = Profile(
            name=name,
            base_url=base_url,
            model=model,
            env=env,
            window=window,
            output=output,
            tools=tools,
            tokenizer=tokenizer,
            calls=calls,
            seconds=seconds,
            output_bytes=output_bytes,
        )
        from .agent import probe

        asyncio.run(probe(p, s))
        s.store_profile(p)
        typer.echo(name)
    except Exception as exc:
        typer.echo(s.clean(str(exc)), err=True)
        raise typer.Exit(1)
    finally:
        s.db.close()


@app.command("eval")
def evaluate_model(
    base_url: str,
    model: str,
    env: str = "",
    cases: Annotated[int, typer.Option(min=1, max=2)] = 2,
):
    """用指定模型运行隔离的 Git 评测，输出 JSON。"""
    import json

    from .evaluation import evaluate

    try:
        profile = Profile(
            name="evaluation",
            base_url=base_url,
            model=model,
            env=env,
            retries=0,
            calls=16,
            seconds=120,
            output=1024,
        )
        if env:
            profile.key(Path.cwd())
        result = asyncio.run(evaluate(profile, cases))
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False))
    if result["passed"] != result["total"]:
        raise typer.Exit(1)
