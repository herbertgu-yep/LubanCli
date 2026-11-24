"""Textual development timeline, model setup and scoped approval dialogs."""

from __future__ import annotations

import asyncio
import json
import os
import re

from rich.markdown import Markdown
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from rich.tree import Tree
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Label,
    Static,
    TextArea,
)

from .dialogs import Inspector, Permission, Setup, report_tree
from .picker import COMMANDS, FilePicker, Picker
from .service import HELP, Service
from .settings import Fields, Settings
from .state import Store, git

SHORTCUTS = "Enter 发送   Ctrl+P 命令   Ctrl+F 文件   Ctrl+R 会话"
ACCENT = "#99d2c5"
MODES = {"develop": "开发", "plan": "计划", "agents": "多 Agent"}


class Composer(TextArea):
    """Keep submission local to the editor so dialogs retain their own Enter key."""

    class Submitted(Message):
        """A deliberate submit keystroke in the composer."""

    def on_key(self, event: events.Key) -> None:
        if event.key not in {"enter", "shift+enter", "ctrl+j", "alt+enter"}:
            return
        event.stop()
        event.prevent_default()
        if event.key == "enter":
            self.post_message(self.Submitted())
        else:
            self.replace("\n", *self.selection)


class LubanApp(App):
    CSS_PATH = "terminal.tcss"
    BINDINGS = [
        Binding("ctrl+s", "submit", "发送", priority=True),
        Binding("ctrl+c", "stop", "停止", priority=True),
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+p", "commands", "命令", priority=True),
        Binding("ctrl+f", "files", "引用文件", priority=True),
        Binding("ctrl+r", "sessions", "会话", priority=True),
        Binding("alt+up", "older", "上一条", priority=True),
        Binding("alt+down", "newer", "下一条", priority=True),
        Binding("escape", "interrupt", "停止"),
        Binding("ctrl+n", "newer", "下一条", priority=True),
    ]

    def __init__(self, s: Store):
        super().__init__()
        self.s = s
        self.svc = Service(s, self.approve, self.notify_event)
        self.job: asyncio.Task | None = None
        self.history: list[str] = []
        self.position = 0
        self.answer: Static | None = None
        self.buffer = ""
        self.edit_preview = ""
        self.stream_output: dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="stage", classes="home"):
            with Vertical(id="column"):
                yield Static(id="bar")
                with Vertical(id="welcome"):
                    yield Static(id="identity")
                    yield Button(id="recent")
                yield VerticalScroll(id="timeline")
                yield Static(id="activity")
                with Vertical(id="compose-box"):
                    with Horizontal(id="compose-line"):
                        yield Static("›", id="chevron")
                        yield Composer(
                            id="prompt", placeholder="描述任务，或用 @ 引用文件…"
                        )
                    yield Static(id="model-line")
                with Horizontal(id="links"):
                    yield Button("命令", id="commands")
                    yield Button("文件", id="files")
                    yield Button("会话", id="sessions")
                    yield Button("模型", id="models")
                yield Static(SHORTCUTS, id="hint")
            with Vertical(id="sidebar"):
                yield Label("工作区", classes="section-label")
                yield Static(id="workspace-info")
                yield Label("当前会话", classes="section-label")
                yield Static(id="session-info")
        yield Static(id="location")

    @on(Button.Pressed, "#recent")
    def recent_session(self):
        if self.can_navigate() and self.recent_sid:
            self.select_command("/resume " + self.recent_sid)

    def on_resize(self, event: events.Resize):
        for stage in self.query("#stage"):
            stage.set_class(event.size.width >= 106 and event.size.height >= 24, "wide")
            stage.set_class(event.size.width < 60, "narrow")
            stage.set_class(event.size.height < 24, "short")

    @on(Button.Pressed, "#links Button")
    def shortcut(self, event):
        {
            "models": self.show_models,
            "files": self.action_files,
            "sessions": self.action_sessions,
            "commands": self.action_commands,
        }[event.button.id]()

    def can_navigate(self):
        return not isinstance(self.screen, ModalScreen) and not (
            self.job and not self.job.done()
        )

    def action_commands(self):
        if self.can_navigate():
            self.push_screen(Picker("命令", COMMANDS), self.select_command)

    def select_command(self, action):
        if not action:
            return
        if action in {"/permissions", "/memory", "/mcp"}:
            self.run_worker(
                Settings(self, self.svc).open(action),
                group="workspace-settings",
                exclusive=True,
            )
            return
        if action == "/worktree":

            def create(fields):
                if fields and fields["name"]:
                    self.select_command("/worktree " + fields["name"])

            self.push_screen(
                Fields("从 HEAD 创建独立工作树", [("name", "任务名", "")]), create
            )
            return
        if action == "/checkpoints":
            rows = self.s.db.execute(
                "SELECT id,path,status FROM file_checkpoint WHERE sid=? ORDER BY rowid DESC",
                (self.svc.sid,),
            )
            self.push_screen(
                Picker(
                    "选择要恢复的文件版本",
                    [
                        ("/rollback " + r[0], r[1], r[0][:8])
                        for r in rows
                        if r[2] == "applied"
                    ],
                ),
                self.select_command,
            )
            return
        if action == "/skills":
            self.job = asyncio.create_task(self.choose_skill())
            return
        if action in {"/model", "/sessions", "/files"}:
            {
                "/model": self.show_models,
                "/sessions": self.action_sessions,
                "/files": self.action_files,
            }[action]()
        else:
            self.job = asyncio.create_task(self.perform(action))

    async def choose_skill(self):
        result = json.loads(await self.svc.command("/skills"))
        choices = [
            (
                "/skills use " + name,
                name,
                ("已启用 · " if name == result["active"] else "")
                + item["description"]
                + " · "
                + item["file"],
            )
            for name, item in result["catalog"].items()
        ]
        if result["active"]:
            choices.append(("/skills off", "停用当前技能", result["active"]))
        self.push_screen(Picker("技能", choices), self.select_command)

    def action_files(self):
        if not self.can_navigate():
            return

        editor = self.query_one(Composer)
        row, column = editor.cursor_location
        prefix = editor.document.get_line(row)[:column]
        mention = re.search(r"(?<!\S)@([^\s@:]*?)$", prefix)
        start = (row, mention.start()) if mention else editor.cursor_location

        def insert(path):
            if path:
                separator = "" if mention or not prefix or prefix[-1].isspace() else " "
                reference = "@" + ('"' + path + '"' if " " in path else path) + " "
                editor.replace(separator + reference, start, (row, column))
                editor.focus()

        box = self.svc.box

        def paths():
            return [os.path.relpath(p, self.s.start) for p in box.files()]

        self.push_screen(FilePicker(paths, mention[1] if mention else ""), insert)

    def action_sessions(self):
        if not self.can_navigate():
            return
        rows = self.s.db.execute(
            """SELECT s.id,s.created,(SELECT body FROM event WHERE sid=s.id AND kind='user'
            ORDER BY id LIMIT 1) AS first_request FROM session s WHERE wid=? ORDER BY s.rowid DESC""",
            (self.s.wid,),
        )
        choices = []
        for sid, created, body in rows:
            title = str(json.loads(body)).splitlines()[0][:80] if body else "新会话"
            choices.append(
                (
                    "/resume " + sid,
                    title,
                    created[:16] + (" · 当前" if sid == self.svc.sid else ""),
                )
            )
        self.push_screen(Picker("恢复会话", choices), self.select_command)

    def action_interrupt(self):
        if not isinstance(self.screen, ModalScreen):
            self.action_stop()

    def show_models(self):
        if not self.can_navigate():
            return
        active = self.s.get("*", "model")
        choices = [
            ("use " + name, name, "当前" if name == active else "")
            for (name,) in self.s.db.execute("SELECT name FROM profile ORDER BY name")
        ]
        choices.append(("add", "＋ 添加模型", "连接端点"))
        if active:
            choices.append(("edit", "编辑当前模型", active))

        def picked(action):
            if action in {"add", "edit"}:
                current = self.s.profile() if action == "edit" else None
                self.push_screen(Setup(self.svc, current), lambda _: self.bar())
            elif action and action.startswith("use "):
                self.s.put("*", "model", action[4:])
                self.bar()

        self.push_screen(Picker("模型", choices), picked)

    def welcome(self, show):
        self.query_one("#stage").set_class(show, "home")
        self.query_one("#bar").display = not show
        self.query_one("#links").display = show
        self.query_one("#location").display = True
        self.query_one("#welcome").display = show
        self.query_one("#timeline").display = not show

    def activity(self, text=""):
        widget = self.query_one("#activity", Static)
        widget.update(Text(text))
        widget.display = bool(text)

    def bar(self):
        mode = self.s.session(self.svc.sid)["mode"]
        branch = git(self.s.root, "branch", "--show-current")
        model = self.s.get("*", "model") or "未配置模型"
        location = os.path.relpath(self.s.start, os.path.expanduser("~"))
        directory = (
            str(self.s.start)
            if location.startswith("..")
            else os.path.join("~", location)
        )
        if self.s.get("*", "model"):
            model = self.s.profile().model
        header = Text(
            "▞  LubanCli", style="bold " + ACCENT, no_wrap=True, overflow="ellipsis"
        )
        header.append("   /   ", style="#777b75")
        header.append(self.s.start.name or directory, style="#dad9ce")
        self.query_one("#bar", Static).update(header)
        meta = Text(
            MODES.get(mode, mode),
            style="bold " + ACCENT,
            no_wrap=True,
            overflow="ellipsis",
        )
        meta.append("   ·   ", style="#777b75")
        meta.append(model, style="#c7cac1")
        self.query_one("#model-line", Static).update(meta)
        location_line = Text("⌁  ", style=ACCENT, no_wrap=True, overflow="ellipsis")
        if branch:
            location_line.append(branch + "   /   ", style="#a8bdad")
        location_line.append(directory, style="#a4aaa3")
        self.query_one("#location", Static).update(location_line)
        self.query_one("#workspace-info", Static).update(
            Text(directory + ("\n" + branch if branch else ""))
        )
        sid = self.svc.sid
        count = self.s.db.execute(
            "SELECT COUNT(*) FROM run WHERE sid=?", (sid,)
        ).fetchone()[0]
        self.query_one("#session-info", Static).update(
            Text(model + "\n" + MODES.get(mode, mode) + f"  ·  {count} 轮")
        )
        identity = Text()
        logo = ("▌   ▌ ▐ ▛▀▖ ▞▀▖ ▛▖▌", "▌   ▌ ▐ ▛▀▖ ▛▀▌ ▌▝▌", "▀▀▘ ▝▀▘ ▀▀  ▘ ▘ ▘ ▘")
        for line in logo:
            for i, c in enumerate(line):
                identity.append(
                    c,
                    style=(
                        "bold "
                        + ("#df9df9" if i < 6 else "#c58ff0" if i < 13 else "#a1a0ff")
                    ),
                )
            identity.append("\n")
        identity.append("LubanCli", style="bold #f4eafd")
        identity.append("  /  " + (self.s.start.name or directory), style="#bab0c6")
        self.query_one("#identity", Static).update(identity)
        recent = self.s.db.execute(
            """SELECT s.id,e.body FROM session s JOIN event e ON s.id=e.sid
            WHERE s.wid=? AND s.id<>? AND e.kind='user' AND e.id=(SELECT min(id) FROM event WHERE sid=s.id AND kind='user')
            ORDER BY (SELECT max(id) FROM event WHERE sid=s.id) DESC LIMIT 1""",
            (self.s.wid, sid),
        ).fetchone()
        button = self.query_one("#recent", Button)
        self.recent_sid = recent[0] if recent else ""
        button.display = bool(recent)
        if recent:
            button.label = Text(
                "继续上次会话  ›  " + str(json.loads(recent[1])).splitlines()[0][:30]
            )

    def on_mount(self):
        self.welcome(True)
        self.activity()
        self.bar()
        self.query_one(TextArea).focus()
        if not self.s.get("*", "model"):
            self.push_screen(Setup(self.svc), lambda _: self.bar())

    async def approve(self, scope, detail):
        future = asyncio.get_running_loop().create_future()
        modal = Permission(scope, detail, self.edit_preview if scope == "edit" else "")
        self.edit_preview = ""

        def finish(value):
            if not future.done():
                future.set_result(value or "deny")

        self.push_screen(modal, finish)
        try:
            return await future
        finally:
            if self.screen is modal:
                self.pop_screen()

    async def entry(self, title, body, *, fold: bool = False):
        timeline = self.query_one("#timeline", VerticalScroll)
        view = Static(
            body if not isinstance(body, str) else Text(body), classes="entry"
        )
        if title == "用户":
            view.add_class("question")
            await timeline.mount(view)
        elif title:
            await timeline.mount(
                Collapsible(
                    view, title=escape(title), collapsed=fold or len(str(body)) > 1200
                )
            )
        else:
            view.add_class("reply")
            await timeline.mount(Static("●  Luban", classes="author"), view)
        timeline.scroll_end(animate=False)
        return view

    async def notify_event(self, kind, text):
        if kind == "edit_preview":
            self.edit_preview = text
            return
        if kind == "delta":
            if self.answer is None:
                self.buffer = ""
                self.activity("● 正在回答   ·   Ctrl+C 停止")
                self.answer = await self.entry("", Text(""))
            self.buffer += text
            self.answer.update(Markdown(self.buffer))
        elif kind in {"stdout", "stderr"}:
            if kind not in self.stream_output:
                self.stream_output[kind] = await self.entry(kind, Text(""))
            view = self.stream_output[kind]
            previous = view.content
            tail = previous.plain if isinstance(previous, Text) else ""
            view.update(Text((tail + text)[-3000:]))
        else:
            self.answer = None
            self.stream_output.clear()
            if kind == "role":
                self.activity("● " + MODES.get(text, text) + "中   ·   Ctrl+C 停止")
                return
            if kind == "action":
                tool, _, payload = text.partition(" ")
                try:
                    args = json.loads(payload)
                    target = next(
                        (args[k] for k in ("path", "text", "pattern") if k in args), ""
                    )
                    action = tool + "  " + str(target)
                except (ValueError, TypeError):
                    action = text.partition("\n")[0]
                self.activity("◌  " + action[:90] + "   ·   Esc 停止")
                return
            if kind == "result":
                tool, _, details = text.partition("\n")
                label = {
                    "command": "命令",
                    "read_file": "读取文件",
                    "edit_file": "编辑文件",
                    "apply_patch": "应用补丁",
                    "create_file": "创建文件",
                    "search": "搜索",
                    "list_dir": "浏览目录",
                    "git_diff": "查看差异",
                }.get(tool, tool)
                body: Text | Syntax = Text(details)
                failed = details.startswith("ERROR:")
                if tool == "command":
                    try:
                        outcome = json.loads(details)
                        label += (
                            f"  {outcome['command']}  ·  退出码 {outcome['exit_code']}"
                        )
                        failed = failed or outcome["exit_code"] != 0
                        body = Text(outcome.get("stdout", ""))
                        if outcome.get("stderr"):
                            body.append("\n" + outcome["stderr"], style="#f1a3ad")
                    except (KeyError, ValueError, TypeError):
                        pass
                elif tool in {"edit_file", "apply_patch", "create_file"}:
                    lines = details.splitlines()
                    target = next((x[6:] for x in lines if x.startswith("+++ b/")), "")
                    added = sum(
                        x.startswith("+") and not x.startswith("+++") for x in lines
                    )
                    removed = sum(
                        x.startswith("-") and not x.startswith("---") for x in lines
                    )
                    if target:
                        label += f"  {target}    +{added} −{removed}"
                    body = Syntax(details, "diff", word_wrap=True)
                elif tool == "read_file" and not failed:
                    lines = details.splitlines()[1:]
                    if lines:
                        label += (
                            "  " + lines[0].partition(":")[0] + f"    {len(lines)} 行"
                        )
                await self.entry(
                    ("× " if failed else "↳ ") + label, body, fold=not failed
                )
                self.activity("● 正在处理工具结果   ·   Ctrl+C 停止")
            elif kind == "outcome":
                await self.entry("运行结果", Text(text))
            elif kind == "run":
                self.activity()
                if text.split(" ", 1)[0] not in {
                    "complete",
                    "answered",
                    "changed",
                    "verified",
                }:
                    await self.entry("执行记录", Text(text))
            else:
                await self.entry(kind, Text(text))

    @on(TextArea.Changed)
    def hint(self, event):
        draft = event.text_area.text
        msg = SHORTCUTS
        event.text_area.styles.height = min(
            6, max(1, event.text_area.wrapped_document.height)
        )
        if draft.startswith("/") and " " not in draft:
            msg = "  ".join(
                x for x in re.findall(r"/[a-z]+", HELP) if x.startswith(draft)
            )
        elif re.search(r"(?:^|\s)@([^\s:]*)$", draft):
            msg = "Ctrl+F 匹配文件   ·   可在路径后加 :起始行-结束行"
        self.query_one("#hint", Static).update(Text(msg))

    @on(Composer.Submitted)
    def action_submit(self):
        if self.job and not self.job.done() or isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one(TextArea)
        text = prompt.text.strip()
        if not text:
            return
        menus = {
            "/": self.action_commands,
            "/sessions": self.action_sessions,
            "/resume": self.action_sessions,
            "/files": self.action_files,
        }
        if text in {
            "/skills",
            "/memory",
            "/permissions",
            "/mcp",
            "/checkpoints",
            "/worktree",
        }:
            prompt.load_text("")
            self.select_command(text)
            return
        if text in menus:
            prompt.load_text("")
            menus[text]()
            return
        if text == "/model":
            prompt.load_text("")
            self.show_models()
            return
        current = self.s.find_profile(self.s.get("*", "model"))
        is_task = not text.startswith("/") or text in {"/continue", "/plan execute"}
        if text in {"/model add", "/model edit"} or (
            is_task and (not current or current.setup_required)
        ):
            if not is_task:
                prompt.load_text("")

            def saved(ok):
                self.bar()
                if ok and is_task:
                    self.call_after_refresh(self.action_submit)

            self.push_screen(
                Setup(self.svc, None if text == "/model add" else current), saved
            )
            return
        self.history.append(text)
        self.position = len(self.history)
        prompt.load_text("")
        self.job = asyncio.create_task(self.perform(text))

    async def perform(self, text):
        try:
            if (
                text in {"/agents on", "/agents off", "/plan on", "/plan off"}
                or text.startswith("/skills use ")
                or text == "/skills off"
            ):
                await self.svc.submit(text)
                mode = self.s.session(self.svc.sid)["mode"]
                message = (
                    "当前模式 · " + MODES.get(mode, mode)
                    if not text.startswith("/skills")
                    else "当前技能 · " + (self.s.get(self.svc.sid, "skill") or "未启用")
                )
                self.notify(message, timeout=2)
                return
            reports = {
                "/checks",
                "/checkpoints",
                "/metrics",
                "/trace",
                "/runtime",
                "/hooks",
                "/symbols",
                "/history",
                "/diff",
                "/context",
                "/help",
                "/plan",
                "/agents",
            }
            if text in reports:
                result = await self.svc.submit(text)
                heading = next(
                    (label for action, label, _ in COMMANDS if action == text), text
                )
                content: Syntax | Tree | Text
                if text == "/diff":
                    content = Syntax(result or "工作区没有改动", "diff", word_wrap=True)
                else:
                    try:
                        content = report_tree(heading, json.loads(result))
                    except ValueError:
                        content = Text(result)
                self.call_after_refresh(self.push_screen, Inspector(heading, content))
                return
            if text in {"/new", "/fork"} or text.startswith("/resume "):
                result = await self.svc.submit(text)
                await self.query_one("#timeline", VerticalScroll).remove_children()
                self.history.clear()
                self.position = 0
                self.query_one(Composer).load_text("")
                self.welcome(text == "/new")
                if text != "/new":
                    await self.restore_timeline()
                    await self.entry("恢复检查", result, fold=True)
                return
            self.welcome(False)
            self.activity("◌ 正在处理   ·   Esc 停止")
            await self.entry("用户", text)
            result = await self.svc.submit(text)
            if text.startswith("/") or self.s.session(self.svc.sid)["mode"] == "agents":
                await self.entry("结果", result)
        except asyncio.CancelledError:
            await self.entry("停止", "已停止本轮。/diff 可查看磁盘与会话编辑记录。")
        except Exception as exc:
            self.welcome(False)
            await self.entry("错误", self.s.clean(str(exc)) or type(exc).__name__)
        finally:
            self.activity()
            self.bar()
            self.answer = None

    async def restore_timeline(self):
        rows = self.s.db.execute(
            """SELECT kind,body FROM (SELECT id,kind,body FROM event
            WHERE sid=? AND kind IN ('user','assistant','result') ORDER BY id DESC LIMIT 100)
            ORDER BY id""",
            (self.svc.sid,),
        )
        for kind, body in rows:
            item = json.loads(body)
            if kind == "user":
                await self.entry("用户", str(item))
            elif kind == "assistant" and item.get("content"):
                await self.entry("", Markdown(item["content"]))
            elif kind == "result":
                await self.notify_event(
                    "result", item["name"] + "\n" + item["text"][:12000]
                )

    def action_stop(self):
        if self.job and not self.job.done():
            self.job.cancel()

    def recall(self, delta: int):
        if self.history and not isinstance(self.screen, ModalScreen):
            self.position = min(len(self.history), max(0, self.position + delta))
            self.query_one(TextArea).load_text(
                self.history[self.position] if self.position < len(self.history) else ""
            )

    def action_older(self):
        self.recall(-1)

    def action_newer(self):
        self.recall(1)

    async def action_quit(self):
        self.action_stop()
        if self.job:
            await asyncio.gather(self.job, return_exceptions=True)
        self.exit()
