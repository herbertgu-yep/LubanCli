import json
import subprocess

import pytest

from lubancli.context import Builder
from lubancli.state import Store, digest


async def test_empty_project_followup(harness):
    svc, replies, requests, events = harness
    replies.extend(
        [
            ("create_file", {"path": "app.py", "content": "print('first')\n"}),
            ("command", {"command": "python3 app.py"}),
            "启动命令是 python3 app.py",
        ]
    )
    await svc.submit("做一个能运行的小项目")
    assert any('"exit_code": 0' in text and "first" in text for _, text in events)
    replies.extend(
        [
            ("read_file", {"path": "app.py"}),
            (
                "edit_file",
                {
                    "path": "app.py",
                    "old": "first",
                    "new": "second",
                    "version": digest(b"print('first')\n"),
                },
            ),
            ("command", {"command": "python3 app.py"}),
            "验证输出 second",
        ]
    )
    await svc.submit("将输出改为 second")
    assert (svc.s.root / "app.py").read_text() == "print('second')\n"
    assert "+print('second')" in await svc.submit("/diff")
    assert "做一个能运行的小项目" in str(requests[-1]["messages"])
    assert all("private reasoning" not in e for kind, e in events if kind == "result")


def init_git(path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
        ).stdout

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (path / "pay.py").write_text("def callback(): return 3\n")
    (path / "notes").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "initial")
    return git


async def test_repository_and_worktrees(harness, tmp_path):
    svc, replies, req, events = harness
    root = svc.s.root
    git = init_git(root)
    (root / "notes").write_text("user edit\n")
    replies.extend([("search", {"text": "callback"}), "入口在 pay.py:1"])
    assert "pay.py:1" in await svc.submit("支付回调从哪里进入")
    assert "pay.py:1" in req[-1]["messages"][-1]["content"]
    replies.extend(
        [
            (
                "edit_file",
                {
                    "path": "pay.py",
                    "version": digest((root / "pay.py").read_bytes()),
                    "old": "return 3",
                    "new": "return 4",
                },
            ),
            (
                "create_file",
                {
                    "path": "test_pay.py",
                    "content": "from pay import callback\nassert callback() == 4\n",
                },
            ),
            ("command", {"command": "python3 test_pay.py"}),
            "已验证",
        ]
    )
    await svc.submit("修改返回值并补测试")
    assert (root / "notes").read_text() == "user edit\n"
    assert "test_pay.py" in await svc.submit("/diff")
    git("switch", "-c", "feature")
    assert "Git 状态已变化" in svc.resume(svc.sid)
    worktree = tmp_path / "other"
    git("worktree", "add", "-b", "separate", str(worktree))
    other = Store(worktree, svc.s.home)
    assert other.wid != svc.s.wid
    with pytest.raises(ValueError):
        other.session(svc.sid)
    nested = root / "sub"
    nested.mkdir()
    link = tmp_path / "link"
    link.symlink_to(nested)
    assert Store(link, svc.s.home).wid == svc.s.wid


async def test_memory_and_compaction(harness, tmp_path):
    svc, replies, req, events = harness
    s = svc.s
    local = await svc.submit("/memory add project test 使用项目测试约定")
    global_id = await svc.submit("/memory add global always 默认用中文解释")
    await svc.submit("/memory add session always 保持旧配置兼容")
    builder = Builder(s, svc.sid)
    p = s.profile()
    other_dir = tmp_path / "else" / s.root.name
    other_dir.mkdir(parents=True)
    b = Store(other_dir, s.home)
    assert {m["id"] for m in b.memories(b.new())} == {global_id}
    assert {m["id"] for m in s.memories(s.new())} == {global_id, local}
    await svc.submit(f"/memory edit {local} 测试改用 pytest")
    assert "pytest" in str(s.memories(svc.sid))
    await svc.submit(f"/memory delete {global_id}")
    assert not b.memories(b.new())
    s.db.execute(
        "UPDATE session SET plan=? WHERE id=?", ('["pending: test migration"]', svc.sid)
    )

    def run(rid):
        s.db.execute("INSERT INTO run VALUES(?,?,?)", (rid, svc.sid, "complete"))

    run("before")
    s.log(svc.sid, "before", "user", "保持旧配置兼容")
    log = "LOG\n" * 10000 + "passed 37 tests"
    s.log(svc.sid, "before", "result", {"name": "command", "text": log})
    run("now")
    for window in [8000, 4500]:
        ctx = builder.build(
            p.model_copy(update={"window": window, "output": 128}),
            "继续项目测试",
            [],
            [],
        )
        msg = str(ctx.messages)
        assert ctx.report["used"] <= ctx.report["budget"]
        assert all(
            x in msg
            for x in (
                "保持旧配置兼容",
                "passed 37 tests",
                "pending: test migration",
                "继续项目测试",
            )
        )
        assert msg.count("LOG") < 150 and ctx.report["summary_until"] > 0
    assert "LOG\\n" * 1000 in await svc.submit("/history 2")
    assert builder.compact()["until"] == ctx.report["summary_until"]


async def test_plan_roles_skill_and_error(harness):
    svc, replies, req, events = harness
    await svc.submit("/skills use review")
    await svc.submit("/plan on")
    step = {"goal": "检查测试", "acceptance": "输出 0", "status": "pending"}
    replies.extend([("set_plan", {"steps": [step], "reason": "初始"}), "计划好了"])
    await svc.submit("先调查")
    assert "command" not in [t["function"]["name"] for t in req[-1]["tools"]]
    assert "审查改动" in str(req[-1]["messages"])
    replies.extend(
        [
            (
                "set_plan",
                {"steps": [{**step, "goal": "检查单元测试"}], "reason": "缩小范围"},
            ),
            "已调整",
        ]
    )
    await svc.submit("调整第一步")
    assert "缩小范围" in await svc.submit("/plan")
    replies.extend(
        [
            ("command", {"command": "python3 -c 'print(42)'"}),
            (
                "set_plan",
                {
                    "steps": [
                        {
                            **step,
                            "status": "done",
                            "evidence": "command exited 0, stdout 42",
                        }
                    ],
                    "reason": "已运行验证",
                },
            ),
            "计划执行完成",
        ]
    )
    assert "计划执行完成" in await svc.submit("/plan execute")
    await svc.submit("/agents on")
    replies.extend(
        [
            ("read_file", {"path": "missing"}),
            "未找到文件",
            ("create_file", {"path": "foo", "content": "ok"}),
            "创建 foo",
            ("git_diff", {}),
            "独立评审完成",
        ]
    )
    assert "独立评审完成" in await svc.submit("执行计划")
    assert [x for k, x in events if k == "role"][-3:] == [
        "Explorer",
        "Implementer",
        "Reviewer",
    ]
    reviewer = req[-1]
    assert "create_file" not in [v["function"]["name"] for v in reviewer["tools"]]
    assert "ERROR:" not in str(reviewer["messages"])
    assert any("ERROR:" in t for k, t in events if k == "result")


async def test_reconcile_and_unknown(harness):
    svc, _, _, _ = harness
    s = svc.s
    s.db.execute("INSERT INTO run VALUES(?,?,?)", ("dead", svc.sid, "running"))
    (s.root / "a").write_text("wrote")
    s.db.execute(
        "INSERT INTO checkpoint VALUES(?,?,?,?,?,?)",
        (
            "dead",
            "c",
            "create_file",
            json.dumps({"arguments": {"path": "a"}, "expected": digest(b"wrote")}),
            "pending",
            "",
        ),
    )
    s.db.execute(
        "INSERT INTO checkpoint VALUES(?,?,?,?,?,?)",
        (
            "dead",
            "d",
            "command",
            '{"arguments":{"command":"touch unexpected"}}',
            "pending",
            "",
        ),
    )
    report = svc.resume(svc.sid)
    assert "reconciled" in report and "unknown" in report
    with pytest.raises(ValueError, match="未确认"):
        await svc.submit("继续")
    assert not (s.root / "unexpected").exists()
    await svc.submit("/ack")
    assert (
        s.db.execute("SELECT status FROM run WHERE id='dead'").fetchone()[0]
        == "interrupted"
    )
