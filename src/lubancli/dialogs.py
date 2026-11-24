"""Interactive permission, inspection and model configuration screens."""

from __future__ import annotations

from rich.syntax import Syntax
from rich.text import Text
from rich.tree import Tree
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Collapsible, Input, Label, Static

from .service import Service
from .state import Profile


class Permission(ModalScreen[str]):
    def on_mount(self):
        self.query_one("#deny", Button).focus()

    BINDINGS = [
        ("escape", "deny", "拒绝"),
        ("1", "allow_once", "允许一次"),
        ("2", "allow_session", "会话内同项"),
    ]

    def __init__(self, scope: str, detail: str, preview: str = ""):
        super().__init__()
        self.preview = preview
        self.scope = scope
        self.detail = detail

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog settings permission"):
            yield Label("授权 · " + self.scope, classes="dialog-title")
            if self.scope == "command":
                yield Static(
                    "宿主 shell 使用当前账号权限，可访问工作区外的文件和网络。"
                )
            yield Static(Text(self.detail), classes="operation")
            if self.preview:
                with VerticalScroll(classes="edit-preview"):
                    yield Static(Syntax(self.preview, "diff", word_wrap=True))
            with Horizontal(classes="buttons"):
                yield Button("允许一次", id="once", variant="primary")
                yield Button("会话内同项", id="session")
                yield Button("拒绝", id="deny")
            yield Label("1 允许一次    2 记住此项    Esc 拒绝", classes="picker-help")

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id)

    def action_allow_once(self):
        self.dismiss("once")

    def action_allow_session(self):
        self.dismiss("session")

    def action_deny(self):
        self.dismiss("deny")


def report_tree(heading: str, data) -> Tree:
    labels = {
        "window": "模型窗口",
        "budget": "可用预算",
        "used": "已使用",
        "count": "计数方式",
        "output": "预留输出",
        "categories": "内容占用",
        "summary_until": "摘要位置",
        "memories": "已载入记忆",
        "records": "记忆记录",
        "last_loaded": "最近载入",
        "read": "工作区",
        "read_policy": "读取策略",
        "instructions_trusted": "项目信任",
        "grants": "会话授权",
        "host_shell": "命令边界",
        "scope": "作用域",
        "body": "内容",
        "trusted": "已信任",
        "args": "参数",
        "command": "启动命令",
    }
    root = Tree(Text(heading), hide_root=True, guide_style="#65516d")

    def populate(parent, value):
        for key, item in (
            value.items() if isinstance(value, dict) else enumerate(value, 1)
        ):
            line = Text(labels.get(str(key), str(key)), style="bold #d5b1e8")
            if isinstance(item, (list, dict)) and item:
                populate(parent.add(line), item)
            else:
                display = "无" if item in ([], {}, None, "") else str(item)
                line.append("   " + display, style="#d5cddd")
                parent.add(line)

    if isinstance(data, (dict, list)) and data:
        populate(root, data)
    else:
        root.add(Text("暂无记录"))
    return root


class Inspector(ModalScreen[None]):
    BINDINGS = [("escape", "back", "返回")]

    def __init__(self, heading, content):
        super().__init__()
        self.heading, self.content = heading, content

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog settings inspector"):
            yield Label(self.heading, classes="dialog-title")
            with VerticalScroll(id="detail-body"):
                yield Static(self.content)
            yield Label("↑ ↓ 滚动    Esc 返回", classes="picker-help")

    def action_back(self):
        self.dismiss(None)


class Setup(ModalScreen[bool]):
    BINDINGS = [("escape", "back", "返回")]

    def __init__(self, svc: Service, profile: Profile | None = None):
        super().__init__()
        self.svc = svc
        self.profile = profile

    def compose(self) -> ComposeResult:
        p = self.profile
        with VerticalScroll(classes="dialog settings"):
            yield Label("连接模型" if not p else "编辑模型", classes="dialog-title")
            if p and p.setup_required:
                yield Label(
                    "请重新填写此模型的 API Key。地址和模型参数已保留。",
                    classes="muted",
                )
            for key, label, value in [
                (
                    "base_url",
                    "API 地址",
                    p.base_url if p else "https://api.openai.com/v1",
                ),
                ("model", "模型 ID", p.model if p else ""),
                ("secret", "API Key", ""),
            ]:
                yield Label(label)
                yield Input(
                    value=value,
                    password=key == "secret",
                    id=key,
                    placeholder=(
                        "已保存 · 留空沿用"
                        if p and p.credential
                        else "粘贴密钥，免密端点可留空"
                    )
                    if key == "secret"
                    else "",
                )
            with Collapsible(title="高级设置", collapsed=True):
                for key, label, value in [
                    ("name", "配置名（默认使用模型 ID）", p.name if p else ""),
                    ("env", "从环境变量读取 API Key", p.env if p else ""),
                    ("window", "上下文窗口", str(p.window) if p else "32768"),
                    ("output", "预留输出", str(p.output) if p else "2048"),
                ]:
                    yield Label(label)
                    yield Input(value=value, id=key)
                yield Checkbox("支持工具调用", p.tools if p else True, id="tools")
            yield Label("", id="feedback")
            with Horizontal(classes="buttons"):
                yield Button("测试并保存", id="save", variant="primary")
                yield Button("返回", id="back")

    def action_back(self):
        self.dismiss(False)

    @on(Input.Submitted)
    def next_field(self, event):
        if event.input.id == "secret":
            self.query_one("#save", Button).press()
        else:
            self.focus_next()

    @on(Button.Pressed)
    async def press(self, event):
        if event.button.id == "back":
            self.action_back()
            return
        btn = event.button
        btn.disabled = True
        status = self.query_one("#feedback", Label)
        try:
            values = {w.id: w.value for w in self.query(Input)}
            secret = values.pop("secret")
            values["name"] = values["name"] or values["model"]
            previous = self.profile
            defaults = (
                previous.model_dump(exclude={"setup_required", "credential"})
                if previous
                else {}
            )
            if (
                previous
                and previous.name == values["name"]
                and previous.base_url.rstrip("/") == values["base_url"].rstrip("/")
                and not values["env"]
                and not secret
            ):
                defaults["credential"] = previous.credential
            profile = Profile.model_validate(
                {**defaults, **values, "tools": self.query_one(Checkbox).value}
            )
            if secret and profile.env:
                raise ValueError("API Key 和环境变量名请只填一项")
            status.update("正在测试端点…")
            await self.svc.save_profile(profile, secret)
        except Exception as exc:
            status.update(Text(self.svc.s.clean(str(exc))))
            btn.disabled = False
        else:
            self.dismiss(True)
