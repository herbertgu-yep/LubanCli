"""Exercise installed console scripts in a real POSIX PTY using a local HTTPX fixture.

Usage: python tests/installed_demo.py /path/to/installed/venv
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import select
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

# Inject the fixture only into the child environment. The installed application
# still performs model setup, streaming, tool validation and execution normally.
TRANSPORT = """
import json, os
from pathlib import Path
import httpx

async def scripted(self, req):
    payload=json.loads(await req.aread())
    assert req.url.host=="fixture.invalid", req.url.host
    message={"role":"assistant","content":None}
    if not payload.get("stream"):
        message["tool_calls"]=[{"id":"probe","type":"function","function":{"name":"ping","arguments":"{}"}}]
        return httpx.Response(200,json={"id":"fixture","model":"fixture","created":1,"object":"chat.completion","choices":[{"index":0,"message":message,"finish_reason":"tool_calls"}]})
    queue=Path(os.environ["LUBAN_TEST_QUEUE"])
    items=json.loads(queue.read_text())
    item=items.pop(0)
    queue.write_text(json.dumps(items))
    if isinstance(item,str):
        delta={"content":item}
    else:
        name,args=item
        delta={"tool_calls":[{"index":0,"id":f"call_{len(items)}","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]}
    obj={"id":"fixture","model":"fixture","created":1,"object":"chat.completion.chunk","choices":[{"index":0,"delta":delta,"finish_reason":"stop" if isinstance(item,str) else "tool_calls"}]}
    return httpx.Response(200,headers={"content-type":"text/event-stream"},text="data: "+json.dumps(obj)+"\\n\\ndata: [DONE]\\n\\n")

httpx.AsyncHTTPTransport.handle_async_request=scripted
"""


def demo(bin_dir: Path, folder: Path):
    work = folder / "empty"
    work.mkdir()
    hooks = folder / "test-transport"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(TRANSPORT)
    data = folder / "config"
    queue = folder / "replies.json"
    source = "print('first')\n"
    queue.write_text(
        json.dumps(
            [
                ["create_file", {"path": "demo.py", "content": source}],
                ["command", {"command": "python3 demo.py"}],
                "First turn complete",
                ["read_file", {"path": "demo.py"}],
                [
                    "edit_file",
                    {
                        "path": "demo.py",
                        "old": "first",
                        "new": "second",
                        "version": hashlib.sha256(source.encode()).hexdigest(),
                    },
                ],
                ["command", {"command": "python3 demo.py"}],
                "Second turn complete",
            ]
        )
    )
    env = {
        **os.environ,
        "LUBAN_DATA_DIR": str(data),
        "LUBAN_TEST_QUEUE": str(queue),
        "PYTHONPATH": str(hooks),
        "LUBAN_FIXTURE_KEY": "test-only",
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TERM": "xterm-256color",
    }

    def cli(*args, check=True):
        return subprocess.run(
            [str(bin_dir / "luban"), *args],
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=check,
            timeout=25,
        )

    assert cli(check=False).returncode == 2
    cli(
        "model-add",
        "fixture",
        "https://fixture.invalid/v1",
        "fixture",
        "--env",
        "LUBAN_FIXTURE_KEY",
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 110, 0, 0))
    process = subprocess.Popen(
        [str(bin_dir / "Luban")],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=work,
        env=env,
        start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    journal = sqlite3.connect(data / "state.sqlite3")
    output = bytearray()

    def wait_for(condition, label):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if condition():
                return
            if process.poll() is not None:
                break
            if select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                    output.extend(chunk)
                except OSError:
                    if process.wait(timeout=3) == 0 and condition():
                        return
                    break
        raise AssertionError(
            f"{label}: runs={journal.execute('SELECT status FROM run').fetchall()}, calls={journal.execute('SELECT name,status,substr(result,1,120) FROM checkpoint').fetchall()}, queue={queue.read_text()}"
        )

    def send(text):
        os.write(master, text.encode())

    def count(sql):
        return journal.execute(sql).fetchone()[0]

    def submit(prompt):
        send("\x1b[200~" + prompt + "\x1b[201~")
        wait_for(lambda: prompt.encode() in output, "prompt painted")
        send("\r")

    def permission(n):
        wait_for(
            lambda: (
                count(
                    "SELECT count(*) FROM checkpoint WHERE name IN ('create_file','edit_file','command')"
                )
                >= n
            ),
            "tool intent",
        )
        scope = "edit" if n in {1, 3} else "command"
        wait_for(
            lambda: (
                ("授权 · " + scope).encode() in output and "允许一次".encode() in output
            ),
            "approval dialog",
        )
        send("1")
        output.clear()

    try:
        wait_for(lambda: "Ctrl+P 命令".encode() in output, "Textual mounted")
        submit("Create a runnable demo.")
        permission(1)
        permission(2)
        wait_for(
            lambda: (
                count("SELECT count(*) FROM run WHERE status IN ('changed','verified')")
                == 1
            ),
            "first turn",
        )
        assert (work / "demo.py").read_text() == source
        wait_for(
            lambda: b"complete " in output or b"First turn complete" in output,
            "assistant answer painted",
        )
        submit("Change it to second.")
        permission(3)
        permission(4)
        wait_for(
            lambda: (
                count("SELECT count(*) FROM run WHERE status IN ('changed','verified')")
                == 2
            ),
            "second turn",
        )
        assert (work / "demo.py").read_text() == "print('second')\n"
        send("\x11")
        wait_for(lambda: process.poll() is not None, "application exit")
        assert process.wait(timeout=5) == 0
        sid = journal.execute("SELECT sid FROM run LIMIT 1").fetchone()[0]
        messages = [
            json.loads(line)
            for line in cli(
                "run", "/diff", "--session", sid, "--json"
            ).stdout.splitlines()
        ]
        assert "+print('second')" in messages[-1]["text"]
        results = [
            json.loads(row[0])
            for row in journal.execute(
                "SELECT result FROM checkpoint WHERE name='command' ORDER BY rowid"
            )
        ]
        assert [(v["exit_code"], v["stdout"].strip()) for v in results] == [
            (0, "first"),
            (0, "second"),
        ]
        print(
            "Installed Luban PTY: two turns, four approvals, outputs first/second, both exit 0. Installed luban: JSON diff and no-TTY exit 2."
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        journal.close()
        os.close(master)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="luban-demo-") as tmp:
        demo(Path(sys.argv[1]).resolve() / "bin", Path(tmp))
