"""Terminal workflows for workspace permissions, memories and tool servers."""

from __future__ import annotations

import json
import shlex

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from .picker import Picker
from .service import Service


class Fields(ModalScreen[dict[str, str] | None]):
    BINDINGS = [("escape", "back", "返回")]

    def __init__(self, title: str, fields: list[tuple[str, str, str]]):
        super().__init__()
        self.heading, self.fields = title, fields

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog settings"):
            yield Label(self.heading, classes="dialog-title")
            for key, label, initial in self.fields:
                yield Label(label)
                yield Input(value=initial, id=key)
            with Horizontal(classes="buttons"):
                yield Button("保存", id="save", variant="primary")
                yield Button("返回", id="back")

    @on(Input.Submitted)
    def advance(self, event):
        if event.input.id == self.fields[-1][0]:
            self.save()
        else:
            self.focus_next()

    @on(Button.Pressed, "#save")
    def save(self):
        self.dismiss(
            {
                key: self.query_one("#" + key, Input).value.strip()
                for key, _, _ in self.fields
            }
        )

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


class Settings:
    def __init__(self, app, service: Service):
        self.app, self.service = app, service

    async def choose(self, heading, rows):
        return await self.app.push_screen_wait(Picker(heading, rows))

    async def confirm(self, heading, detail):
        return (
            await self.choose(
                heading, [("back", "返回", "保留当前设置"), ("yes", "确认", detail)]
            )
            == "yes"
        )

    async def inspect(self, heading, value):
        from .dialogs import Inspector, report_tree

        content = (
            report_tree(heading, value)
            if isinstance(value, (list, dict))
            else Text(str(value))
        )
        await self.app.push_screen_wait(Inspector(heading, content))

    async def open(self, command):
        page = {
            "/memory": self.memory,
            "/mcp": self.servers,
            "/permissions": self.permissions,
        }[command]
        while True:
            try:
                data = json.loads(await self.service.command(command))
                if not await page(data):
                    return
            except Exception as exc:
                await self.inspect(
                    "操作未完成", self.service.s.clean(str(exc)) or type(exc).__name__
                )
                return

    async def permissions(self, data):
        ask = data["read_policy"] == "ask"
        trust = data["instructions_trusted"]
        action = await self.choose(
            "工作区权限",
            [
                ("details", "权限详情", data["read"]),
                ("policy", "操作模式", data["policy"]),
                ("sandbox", "命令隔离", data["sandbox"]),
                (
                    "read allow" if ask else "read ask",
                    "读取前询问",
                    "已开启 · 选择关闭" if ask else "已关闭 · 选择开启",
                ),
                (
                    "untrust" if trust else "trust",
                    "使用项目说明",
                    "已信任 · 选择撤销" if trust else "选择信任当前工作区",
                ),
                ("reset", "清除本会话的工具授权", f"{len(data['grants'])} 项"),
            ],
        )
        if not action:
            return False
        if action == "details":
            await self.inspect("权限详情", data)
        elif action == "policy":
            policy = await self.choose(
                "选择本会话的操作模式",
                [
                    ("ask", "逐项授权", "命令、编辑和外部工具"),
                    ("readonly", "只读调查", "禁止写入、命令和外部工具"),
                    ("auto", "自动编辑", "命令和外部工具仍需授权"),
                ],
            )
            if policy:
                await self.service.command("/policy " + policy)
        elif action == "sandbox":
            backend = await self.choose(
                "命令执行环境",
                [
                    ("on", "系统隔离", "不可用时执行失败"),
                    ("off", "宿主 shell", "使用当前账号的权限"),
                ],
            )
            if backend:
                await self.service.command("/sandbox " + backend)
        elif action not in {"reset", "trust", "read allow"} or await self.confirm(
            "更改权限", action + " · " + data["read"]
        ):
            await self.service.command("/permissions " + action)
        return True

    async def memory(self, data):
        scopes = {"project": "当前项目", "session": "当前会话", "global": "所有项目"}
        records = {item["id"]: item for item in data["records"]}
        action = await self.choose(
            "记忆",
            [
                ("add", "＋ 添加记忆", "选择作用域"),
                ("loaded", "本轮载入", f"{len(data['last_loaded'])} 项"),
                *(
                    (key, item["body"], scopes.get(item["scope"], item["scope"]))
                    for key, item in records.items()
                ),
            ],
        )
        if not action:
            return False
        if action == "loaded":
            await self.inspect("本轮载入的记忆", data["last_loaded"])
        elif action == "add":
            scope = await self.choose(
                "记忆适用范围", [(k, v, "") for k, v in scopes.items()]
            )
            if scope:
                fields = await self.app.push_screen_wait(
                    Fields(
                        "添加记忆 · " + scopes[scope],
                        [("body", "内容", ""), ("tag", "检索标签（可选）", "")],
                    )
                )
                if fields and fields["body"]:
                    await self.service.command(
                        shlex.join(
                            ["/memory", "add", scope, fields["tag"], fields["body"]]
                        )
                    )
        else:
            item = records[action]
            choice = await self.choose(
                scopes[item["scope"]],
                [
                    ("view", "查看内容和来源", item["body"]),
                    ("edit", "编辑内容", ""),
                    ("delete", "删除记忆", ""),
                ],
            )
            if choice == "view":
                await self.inspect("记忆详情", item)
            elif choice == "edit":
                fields = await self.app.push_screen_wait(
                    Fields("编辑记忆", [("body", "内容", item["body"])])
                )
                if fields and fields["body"]:
                    await self.service.command(
                        shlex.join(["/memory", "edit", action, fields["body"]])
                    )
            elif choice == "delete" and await self.confirm(
                "删除这条记忆？", item["body"]
            ):
                await self.service.command("/memory delete " + action)
        return True

    async def servers(self, data):
        action = await self.choose(
            "MCP 工具服务",
            [
                ("+", "＋ 配置服务", "stdio"),
                ("?", "发现可用工具", "已信任的服务"),
                *(
                    (
                        name,
                        name,
                        ("已信任" if info["trusted"] else "待信任")
                        + " · "
                        + info["command"],
                    )
                    for name, info in data.items()
                ),
            ],
        )
        if not action:
            return False
        if action == "+":
            fields = await self.app.push_screen_wait(
                Fields(
                    "配置 stdio 服务",
                    [("name", "服务名", ""), ("launch", "启动命令和参数", "")],
                )
            )
            if fields and fields["name"] and fields["launch"]:
                await self.service.command(
                    shlex.join(
                        ["/mcp", "add", fields["name"], *shlex.split(fields["launch"])]
                    )
                )
        elif action == "?":
            await self.inspect(
                "可用工具", json.loads(await self.service.command("/mcp tools"))
            )
        else:
            info = data[action]
            command = shlex.join([info["command"], *info["args"]])
            change = "stop" if info["trusted"] else "trust"
            choice = await self.choose(
                action,
                [
                    ("details", "查看启动配置", command),
                    (change, "撤销信任" if info["trusted"] else "信任此服务", command),
                ],
            )
            if choice == "details":
                await self.inspect(action, info)
            elif choice == change:
                await self.service.command(shlex.join(["/mcp", change, action]))
        return True
