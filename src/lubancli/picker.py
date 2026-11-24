"""Searchable terminal lists with a shared keyboard interaction."""

import asyncio
from collections.abc import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList


class Picker(ModalScreen[str]):
    """Keep the filter focused while arrow keys move the selected result."""

    BINDINGS = [
        Binding("escape", "back", "返回"),
        Binding("up", "step(-1)", "上一项", priority=True),
        Binding("down", "step(1)", "下一项", priority=True),
    ]

    def __init__(self, heading: str, rows: list[tuple[str, str, str]]):
        super().__init__()
        self.heading = heading
        self.rows = rows
        self.filtered_rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog settings"):
            yield Label(self.heading, classes="dialog-title")
            yield Input(placeholder="输入名称或关键词…", id="filter")
            yield OptionList(id="choices")
            yield Label("", id="matches", classes="muted")
            yield Label("↑ ↓ 选择    Enter 确认    Esc 返回", classes="picker-help")

    def on_mount(self):
        self.filter_rows("")
        self.query_one(Input).focus()

    @on(Input.Changed)
    def changed(self, event):
        self.filter_rows(event.value)

    def filter_rows(self, value):
        words = value.casefold().split()
        self.filtered_rows = [
            row
            for row in self.rows
            if all(w in " ".join(row).casefold() for w in words)
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        for _, title, detail in self.filtered_rows[:150]:
            line = Text(title, no_wrap=True, overflow="ellipsis")
            if detail:
                line.append("    " + detail, style="#aaa4b6")
            options.add_option(line)
        options.highlighted = 0 if self.filtered_rows else None
        self.query_one("#matches", Label).update(
            "没有匹配项"
            if not self.filtered_rows
            else f"{len(self.filtered_rows)} 项"
            + (" · 显示前 150 项" if len(self.filtered_rows) > 150 else "")
        )

    def action_step(self, delta: int):
        options = self.query_one(OptionList)
        if delta > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    @on(Input.Submitted)
    def submit(self):
        index = self.query_one(OptionList).highlighted
        if index is not None:
            self.dismiss(self.filtered_rows[index][0])

    @on(OptionList.OptionSelected)
    def select_row(self, event):
        self.dismiss(self.filtered_rows[event.option_index][0])

    def action_back(self):
        self.dismiss("")


class FilePicker(Picker):
    """Show the filter immediately while the workspace is being indexed."""

    def __init__(self, paths: Callable[[], list[str]], query: str):
        super().__init__("引用工作区文件", [])
        self.paths, self.initial_query = paths, query

    def on_mount(self):
        super().on_mount()
        field = self.query_one(Input)
        field.value = self.initial_query
        field.cursor_position = len(field.value)
        self.query_one("#matches", Label).update("正在读取工作区…")
        self.run_worker(self.load_paths())

    async def load_paths(self):
        try:
            paths = await asyncio.to_thread(self.paths)
        except OSError as exc:
            self.query_one("#matches", Label).update(Text(str(exc)))
            return
        self.rows = [(path, path, "") for path in paths]
        self.filter_rows(self.query_one(Input).value)


COMMANDS = [
    ("/worktree", "新建任务工作树", "工作区"),
    ("/fork", "分叉当前会话", "会话"),
    ("/symbols", "代码符号地图", "工作区"),
    ("/checkpoints", "文件检查点", "工作区"),
    ("/checks", "发现项目验证命令", "工作区"),
    ("/metrics", "运行统计", "会话"),
    ("/trace", "运行轨迹", "会话"),
    ("/new", "新建会话", "会话"),
    ("/sessions", "恢复会话", "会话"),
    ("/model", "选择模型", "配置"),
    ("/files", "引用文件", "工作区"),
    ("/diff", "查看改动", "工作区"),
    ("/plan on", "制定计划", "工作模式"),
    ("/plan execute", "执行当前计划", "工作模式"),
    ("/plan off", "返回开发模式", "工作模式"),
    ("/agents on", "开启多 Agent", "工作模式"),
    ("/agents off", "退出多 Agent", "工作模式"),
    ("/context", "上下文用量", "会话"),
    ("/compact", "压缩上下文", "会话"),
    ("/memory", "查看记忆", "会话"),
    ("/skills", "查看技能", "扩展"),
    ("/mcp", "查看工具服务", "扩展"),
    ("/permissions", "查看权限", "工作区"),
    ("/history", "操作记录", "会话"),
    ("/continue", "继续中断的任务", "会话"),
    ("/help", "全部命令与参数", "帮助"),
]
