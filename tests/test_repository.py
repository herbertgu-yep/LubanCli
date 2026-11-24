import json

from lubancli.repository import RepositoryMap


async def test_symbol_map_incremental_references_and_context(harness, monkeypatch):
    svc, replies, requests, _ = harness
    root = svc.s.root
    (root / "lib.py").write_text("def handle(): pass\n")
    consumer = root / "app.py"
    consumer.write_text("from lib import handle as run\nrun()\n")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("def handle(): pass")
    index = RepositoryMap(svc.box)
    assert index.query("lib.handle") == [
        "app.py:1  import lib.handle",
        "app.py:2  call lib.handle",
        "lib.py:1  def lib.handle",
    ]
    import lubancli.repository as repository

    parsed = []
    parse = repository.symbols

    def count(source, path):
        parsed.append(path)
        return parse(source, path)

    monkeypatch.setattr(repository, "symbols", count)
    consumer.write_text("from lib import handle\n\nhandle()\n")
    assert "app.py:3  call lib.handle" in index.query("handle")
    assert parsed == ["app.py"]
    replies.append("lib.py:1")
    await svc.submit("locate handle")
    assert "lib.py:1" in json.dumps(requests[0]["messages"])


def test_launch_directory_is_independent_of_inherited_git_env(tmp_path, monkeypatch):
    import subprocess

    from lubancli.state import Store

    foreign = tmp_path / "foreign"
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(foreign)], check=True)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    store = Store(project, tmp_path / "state")
    assert store.root == project.resolve()
    store.db.close()
