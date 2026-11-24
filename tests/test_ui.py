import asyncio

from textual.widgets import TextArea

from lubancli.ui import LubanApp, Permission, Setup


async def test_setup_preserves_draft_and_two_turns(harness):
    svc, replies, _, _ = harness
    svc.s.put("*", "model", "")
    app = LubanApp(svc.s)
    async with app.run_test(size=(64, 23)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, Setup)
        await pilot.press("escape")
        prompt = app.query_one(TextArea)
        prompt.load_text("生成文件")
        await pilot.press("enter")
        assert isinstance(app.screen, Setup)
        await pilot.press("escape")
        assert prompt.text == "生成文件"
        svc.s.put("*", "model", "test")
        replies.extend(
            [
                ("create_file", {"path": "hello.py", "content": "print(1)\n"}),
                "运行 python3 hello.py",
            ]
        )
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Permission)
        await pilot.click("#once")
        await asyncio.wait_for(app.job, 5)
        assert (svc.s.root / "hello.py").is_file()
        replies.extend([("read_file", {"path": "hello.py"}), "代码在 hello.py:1"])
        prompt.load_text("解释刚才的文件")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        await pilot.press("alt+up")
        assert prompt.text == "解释刚才的文件"


async def test_stop_preserves_unsent_input(harness):
    svc, replies, _, _ = harness
    app = LubanApp(svc.s)
    async with app.run_test() as pilot:
        prompt = app.query_one(TextArea)
        replies.append(("command", {"command": "sleep 8"}))
        prompt.load_text("运行命令")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#once")
        await pilot.pause()
        prompt.load_text("下一轮草稿")
        await pilot.press("ctrl+c")
        await asyncio.wait_for(app.job, 3)
        assert prompt.text == "下一轮草稿"
        assert svc.s.db.execute("SELECT status FROM run").fetchone()[0] == "cancelled"


async def test_composer_multiline_and_home_shortcuts(harness):
    from textual import events

    svc, replies, requests, _ = harness
    app = LubanApp(svc.s)
    async with app.run_test(size=(48, 18)) as pilot:
        editor = app.query_one(TextArea)
        assert app.query_one("#welcome").display
        editor.load_text("一")
        editor.move_cursor((0, 1))
        await pilot.press("shift+enter")
        app.post_message(events.Paste("二\n三"))
        await pilot.pause()
        await pilot.press("ctrl+j")
        editor.insert("四")
        assert editor.text == "一\n二\n三\n四"
        assert app.job is None
        replies.append("收到")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        assert requests[-1]["messages"][-1]["content"] == "一\n二\n三\n四"
        assert not app.query_one("#welcome").display
        editor.load_text("/new")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        assert app.query_one("#welcome").display
        await pilot.click("#sessions")
        from lubancli.picker import Picker

        assert isinstance(app.screen, Picker)
        await pilot.press("escape")


async def test_home_and_session_share_a_responsive_editor(harness):
    svc, replies, _, _ = harness
    app = LubanApp(svc.s)
    async with app.run_test(size=(100, 32)) as pilot:
        editor = app.query_one(TextArea)
        home_y = editor.region.y
        assert app.query_one("#identity").region.bottom < home_y
        assert home_y >= app.size.height - 10
        replies.append("收到")
        editor.load_text("你好")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        await pilot.pause()
        assert app.query_one(TextArea) is editor
        assert editor.has_focus
        assert editor.region.y >= app.size.height - 9
        editor.load_text("/new")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        await pilot.pause()
        assert editor.region.y == home_y


async def test_legacy_setup_saves_local_key_and_sends_draft(harness, monkeypatch):
    import json
    import stat

    from textual.widgets import Input

    svc, replies, requests, _ = harness
    data = svc.s.profile().model_dump(exclude={"credential", "setup_required"})
    data["use_keyring"] = True
    svc.s.db.execute("UPDATE profile SET body=?", (json.dumps(data),))
    checked = []

    async def probe(p, s, secret):
        checked.append((p.model, secret))

    monkeypatch.setattr("lubancli.service.probe", probe)
    app = LubanApp(svc.s)
    async with app.run_test(size=(80, 30)) as pilot:
        editor = app.query_one(TextArea)
        editor.load_text("你好")
        await pilot.press("enter")
        assert isinstance(app.screen, Setup)
        assert app.screen.query_one("#model", Input).value == "test"
        app.screen.query_one("#secret", Input).value = "fixture-local-key"
        replies.append("收到")
        await pilot.click("#save")
        await pilot.pause()
        await asyncio.wait_for(app.job, 5)
        assert requests[-1]["messages"][-1]["content"] == "你好"
        assert checked == [("test", "fixture-local-key")]
        p = svc.s.profile()
        path = svc.s.home / "credentials" / (p.credential + ".json")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert p.key(svc.s.home) == "fixture-local-key"
        assert (
            "fixture-local-key"
            not in svc.s.db.execute("SELECT body FROM profile").fetchone()[0]
        )
        await svc.command("/model delete test")
        assert not path.exists()


async def test_first_run_local_key_reaches_sdk_and_survives_restart(
    tmp_path, monkeypatch
):
    import json

    import httpx
    from textual.widgets import Input

    from lubancli.state import Store

    data = tmp_path / "data"
    work = tmp_path / "project"
    work.mkdir()
    requests = []

    async def respond(transport, request):
        body = json.loads(request.content)
        requests.append((body, request.headers["authorization"]))
        if body.get("stream"):
            chunk = {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local",
                "choices": [
                    {"index": 0, "delta": {"content": "收到"}, "finish_reason": "stop"}
                ],
            }
            return httpx.Response(
                200,
                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "local",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "ping",
                                    "type": "function",
                                    "function": {"name": "ping", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    s = Store(work, data)
    app = LubanApp(s)
    async with app.run_test(size=(58, 22)) as pilot:
        await pilot.pause()
        app.screen.query_one("#model", Input).value = "local"
        app.screen.query_one("#secret", Input).value = "ui-test-only"
        await pilot.click("#save")
        await pilot.pause()
        assert not isinstance(app.screen, Setup)
        editor = app.query_one(TextArea)
        editor.load_text("你好")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        assert len(requests) == 2
        assert all(auth == "Bearer ui-test-only" for _, auth in requests)
    s.db.close()
    reopened = Store(work, data)
    assert reopened.profile().key(reopened.home) == "ui-test-only"
    reopened.db.close()


async def test_keyboard_pickers_keep_draft_and_restore_transcript(harness):
    from textual.widgets import Input, Static

    from lubancli.picker import Picker
    from lubancli.ui import Inspector

    svc, replies, _, _ = harness
    (svc.s.start / "hello world.py").write_text("print(1)")
    app = LubanApp(svc.s)
    async with app.run_test(size=(112, 32)) as pilot:
        editor = app.query_one(TextArea)
        assert app.query_one("#sidebar").region.width > 0
        editor.insert("查看 @hel")
        await pilot.press("ctrl+f")
        assert app.screen.query_one(Input).value == "hel"
        app.screen.query_one(Input).value = "hello"
        await pilot.press("enter")
        assert editor.text == '查看 @"hello world.py" '
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, Picker)
        app.screen.query_one(Input).value = "/diff"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Inspector)
        await pilot.press("escape")
        assert editor.text.startswith("查看")
        editor.load_text("这个项目怎样启动")
        replies.append("运行 python main.py")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        original = app.svc.sid
        editor.load_text("/new")
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        assert original != app.svc.sid
        await pilot.press("ctrl+r")
        app.screen.query_one(Input).value = "怎样启动"
        await pilot.pause()
        await pilot.press("enter")
        await asyncio.wait_for(app.job, 5)
        assert original == app.svc.sid
        assert "这个项目怎样启动" in str(app.query_one(".question", Static).content)
        assert app.query(".reply")
        await pilot.resize_terminal(50, 18)
        await pilot.pause()
        assert app.query_one("#sidebar").region.width == 0


async def test_workspace_settings_add_memory_and_confirm_permission_change(harness):
    from textual.widgets import Input

    from lubancli.picker import Picker
    from lubancli.settings import Fields

    svc, _, _, _ = harness
    app = LubanApp(svc.s)
    async with app.run_test(size=(72, 24)) as pilot:
        editor = app.query_one(TextArea)

        async def select(query):
            assert isinstance(app.screen, Picker)
            app.screen.query_one(Input).value = query
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        editor.load_text("/memory")
        await pilot.press("enter")
        await select("添加")
        await select("当前项目")
        assert isinstance(app.screen, Fields)
        app.screen.query_one("#body", Input).value = "旧版配置也要能读取"
        await pilot.click("#save")
        await pilot.pause()
        assert svc.s.memories(app.svc.sid)[0]["body"] == "旧版配置也要能读取"
        await pilot.press("escape")
        editor.load_text("/permissions")
        await pilot.press("enter")
        await select("使用项目说明")
        await pilot.press("escape")
        assert svc.s.get(svc.s.wid, "trust") != "yes"
        await select("使用项目说明")
        await select("确认")
        assert svc.s.get(svc.s.wid, "trust") == "yes"
        await pilot.press("escape")
        editor.load_text("/mcp")
        await pilot.press("enter")
        await select("配置服务")
        app.screen.query_one("#name", Input).value = "syntax"
        app.screen.query_one("#launch", Input).value = "python -m lubancli.example_mcp"
        await pilot.click("#save")
        await pilot.pause()
        result = await app.svc.command("/mcp")
        assert '"syntax"' in result
        assert '"trusted": false' in result or '"trusted":false' in result
