import json
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI

from lubancli.service import Service
from lubancli.state import Profile, Store


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """A scripted HTTP transport below the official SDK. Files and tools are real."""
    root = tmp_path / "project"
    root.mkdir()
    store = Store(root, tmp_path / "data")
    assert store.root == root.resolve()
    p = Profile(
        name="test", model="test", base_url="https://model.invalid/v1", window=36000
    )
    store.db.execute("INSERT INTO profile VALUES(?,?)", (p.name, p.model_dump_json()))
    store.put("*", "model", p.name)
    events = []
    requests = []
    replies = []

    async def notify(kind, text):
        events.append((kind, text))

    async def allow(scope, detail):
        events.append(("permission", scope + " " + detail))
        return "once"

    def handler(req):
        request = json.loads(req.content)
        requests.append(request)
        reply = replies.pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, str):
            delta = {"content": reply}
        else:
            name, args = reply
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call-{len(requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            }
        data = {
            "id": "test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "stop" if isinstance(reply, str) else "tool_calls",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n",
        )

    @asynccontextmanager
    async def client(*args):
        async with AsyncOpenAI(
            api_key="fixture",
            base_url=p.base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as sdk:
            yield sdk

    monkeypatch.setattr("lubancli.agent.client_for", client)
    yield Service(store, allow, notify), replies, requests, events
    store.db.close()
