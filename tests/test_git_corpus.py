"""Replay focused regressions against source files from the product's Git history."""

import json
import subprocess
from pathlib import Path

import pytest

from lubancli.service import Service
from lubancli.state import Store, digest


@pytest.mark.parametrize(
    ("broken", "fixed", "assertion"),
    [
        (
            "return uuid.uuid4().hex[:8]",
            "return uuid.uuid4().hex",
            "assert len(uid()) == 32",
        ),
        (
            "hashlib.sha1(data).hexdigest()",
            "hashlib.sha256(data).hexdigest()",
            "assert digest(b'x') == hashlib.sha256(b'x').hexdigest()",
        ),
    ],
)
async def test_real_repository_patch(harness, tmp_path, broken, fixed, assertion):
    service, replies, _, _ = harness
    source = Path(__file__).resolve().parents[1]
    origin = source if (source / ".git").exists() else source / "LubanCli.bundle"
    if not origin.exists():
        pytest.skip("此源码分发不含 Git 历史")
    work = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", str(origin), str(work)], check=True
    )

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(work), *args], check=True, capture_output=True, text=True
        ).stdout

    git("sparse-checkout", "init", "--no-cone")
    git("sparse-checkout", "set", "--no-cone", "/src/lubancli/state.py")
    git("checkout", "-q", "HEAD")
    target = work / "src/lubancli/state.py"
    baseline = target.read_bytes()
    assert fixed in baseline.decode()
    target.write_text(baseline.decode().replace(fixed, broken, 1))
    tests = work / "tests"
    tests.mkdir()
    (tests / "test_contract.py").write_text(
        "from pathlib import Path\nimport hashlib\nimport sys\nsys.path.insert(0,str(Path('src/lubancli').resolve()))\nfrom state import digest,uid\n\ndef test_contract():\n    "
        + assertion
        + "\n"
    )
    store = Store(work, service.s.home)
    try:
        session = Service(store, service.approve, service.notify)
        replies.extend(
            [
                (
                    "edit_file",
                    {
                        "path": "src/lubancli/state.py",
                        "old": broken,
                        "new": fixed,
                        "version": digest(target.read_bytes()),
                    },
                ),
                "repaired",
            ]
        )
        await session.submit("恢复标识符与原有持久化格式的兼容性，并运行验证")
        assert target.read_bytes() == baseline
        assert not git("diff", "--", "src/lubancli/state.py")
        trace = json.loads(await session.submit("/trace"))
        assert any(
            row["event"] == "verification" and row["data"]["exit_code"] == 0
            for row in trace
        )
    finally:
        store.db.close()
