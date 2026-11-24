"""Run deterministic repair tasks against committed repositories and real checks."""

import json
import subprocess

import pytest

from lubancli.state import digest


@pytest.mark.parametrize(("threshold", "expected"), [(3, 3), (7, 7)])
async def test_git_repair_benchmark(harness, threshold, expected):
    svc, replies, _, _ = harness
    root = svc.s.root
    target = root / "quota.py"
    target.write_text("def allowed(n):\n    return n == 0\n")
    (root / "tests").mkdir()
    (root / "tests/test_quota.py").write_text(
        f"from quota import allowed\n\ndef test_boundary():\n    assert allowed({expected})\n    assert not allowed({expected + 1})\n"
    )

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "baseline",
    )
    before = target.read_text()
    wrong = before.replace("== 0", f"< {threshold}")
    right = wrong.replace("< ", "<= ")

    def edit(old, new):
        return (
            "edit_file",
            {
                "path": "quota.py",
                "old": old,
                "new": new,
                "version": digest(old.encode()),
            },
        )

    replies.extend(
        [edit(before, wrong), "implemented", edit(wrong, right), "corrected boundary"]
    )
    result = await svc.submit(f"Make the quota inclusive at {threshold}.")
    assert "python -m pytest -q → 0" in result
    assert target.read_text() == right
    trace = json.loads(await svc.submit("/trace"))
    assert [r["data"]["exit_code"] for r in trace if r["event"] == "verification"] == [
        1,
        0,
    ]
    assert json.loads(await svc.submit("/metrics"))["success_rate"] == 1


async def test_failed_verification_marks_run_failed(harness):
    svc, replies, _, _ = harness
    await svc.submit("/runtime repair_rounds 0")
    root = svc.s.root
    (root / "tests").mkdir()
    (root / "tests/test_contract.py").write_text("def test_contract(): assert False\n")
    replies.extend(
        [("create_file", {"path": "feature.py", "content": "value=1\n"}), "done"]
    )
    with pytest.raises(RuntimeError, match="验证失败"):
        await svc.submit("实现功能并验证")
    assert json.loads(await svc.submit("/metrics"))["runs"] == {"failed": 1}
