import json
import subprocess

import pytest

from lubancli.state import digest


async def test_checkpoint_fork_and_isolated_worktree(harness):
    svc, replies, _, _ = harness
    path = svc.s.root / "entry.py"
    path.write_text("# user edit\nvalue = 2\n")
    subprocess.run(["git", "init", "-q", str(svc.s.root)], check=True)

    def git(*args):
        subprocess.run(
            ["git", "-C", str(svc.s.root), *args], check=True, capture_output=True
        )

    git("add", "entry.py")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    path.write_text(path.read_text() + "# pending user change\n")
    original = path.read_bytes()
    replies.extend(
        [
            (
                "edit_file",
                {
                    "path": "entry.py",
                    "old": "2",
                    "new": "3",
                    "version": digest(original),
                },
            ),
            "done",
        ]
    )
    await svc.submit("change value")
    ref = json.loads(await svc.submit("/checkpoints"))[0]["id"]
    changed = path.read_bytes()
    path.write_text("other edit")
    with pytest.raises(ValueError, match="变化"):
        await svc.submit("/rollback " + ref)
    assert path.read_text() == "other edit"
    path.write_bytes(changed)
    await svc.submit("/rollback " + ref)
    assert path.read_bytes() == original
    await svc.submit("/memory add session always 保持兼容")
    parent = svc.sid
    child = await svc.submit("/fork")
    assert child != parent and "change value" in await svc.submit("/history")
    assert svc.s.memories(child)[0]["body"] == "保持兼容"
    assert json.loads(await svc.submit("/checkpoints")) == []
    from pathlib import Path

    tree = Path(await svc.submit("/worktree task"))
    assert (tree / "entry.py").read_bytes() != original
    assert path.read_bytes() == original


async def test_patch_checks_context_and_restores_exact_newline(harness):
    svc, replies, _, _ = harness
    target = svc.s.root / "values.txt"
    target.write_bytes(b"a\nb")
    patch = "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n"
    args = {
        "path": "values.txt",
        "version": digest(target.read_bytes()),
        "patch": patch,
    }
    replies.extend([("apply_patch", args), "complete"])
    await svc.submit("change b to c")
    assert target.read_bytes() == b"a\nc"
    ref = json.loads(await svc.submit("/checkpoints"))[0]["id"]
    with pytest.raises(ValueError, match="版本"):
        await svc.box.call("apply_patch", args)
    await svc.submit("/rollback " + ref)
    assert target.read_bytes() == b"a\nb"
    with pytest.raises(ValueError, match="冲突"):
        await svc.box.call("apply_patch", {**args, "patch": patch.replace("-b", "-z")})


async def test_reconcile_snapshot_in_write_journal_gap(harness):
    from lubancli.checkpoints import capture

    svc, _, _, _ = harness
    store = svc.s
    target = store.root / "result"
    store.db.execute(
        "INSERT INTO run VALUES(?,?,?)", ("interrupted", svc.sid, "running")
    )
    expected = digest(b"saved")
    args = json.dumps(
        {
            "arguments": {"path": "result", "content": "saved"},
            "path": "result",
            "expected": expected,
        }
    )
    store.db.execute(
        "INSERT INTO checkpoint VALUES(?,?,?,?,?,?)",
        ("interrupted", "write", "create_file", args, "pending", ""),
    )
    ref = capture(store, svc.sid, target, None, b"saved", ("interrupted", "write"))
    target.write_bytes(b"saved")
    target.chmod(0o600)
    assert "reconciled" in svc.resume(svc.sid)
    await svc.submit("/rollback " + ref)
    assert not target.exists()
