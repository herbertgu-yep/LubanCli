"""Restore a single tool edit only while its exact postimage is still present."""

import os
import tempfile
from pathlib import Path

from filelock import FileLock

from .state import digest, uid


def edit_id(rid: str, call_id: str) -> str:
    return digest((rid + "\0" + call_id).encode())


def capture(store, sid, path, before, after, journal=None):
    ref = edit_id(*journal) if journal else uid()
    mode = path.stat().st_mode & 0o777 if before is not None else 0o600
    store.db.execute(
        "INSERT INTO file_checkpoint VALUES(?,?,?,?,?,?,?,?)",
        (
            ref,
            sid,
            str(path.relative_to(store.root)),
            before,
            mode,
            digest(after),
            mode,
            "pending",
        ),
    )
    return ref


def restore(box, ref):
    store = box.s
    row = store.db.execute(
        "SELECT * FROM file_checkpoint WHERE id=? AND sid=?", (ref, box.sid)
    ).fetchone()
    if not row:
        raise ValueError("当前会话没有此检查点")
    with FileLock(str(store.home / (store.wid + ".lock")), timeout=3):
        path = store.root / row["path"]
        if box.path(str(path)) != path:
            raise ValueError("文件路径已经变化")
        if row["status"] == "restored":
            return "此检查点已恢复"
        if row["status"] == "restoring":
            already_restored = (
                not path.exists()
                if row["before"] is None
                else path.is_file()
                and path.read_bytes() == row["before"]
                and path.stat().st_mode & 0o777 == row["mode"]
            )
            if already_restored:
                store.db.execute(
                    "UPDATE file_checkpoint SET status='restored' WHERE id=?", (ref,)
                )
                return "已核对恢复结果 " + row["path"]
        if row["status"] not in {"applied", "restoring"}:
            raise ValueError("写入未确认，不能自动回滚")
        if (
            not path.is_file()
            or path.stat().st_mode & 0o777 != row["after_mode"]
            or digest(path.read_bytes()) != row["after_hash"]
        ):
            raise ValueError("文件已变化，保留当前内容")
        store.db.execute(
            "UPDATE file_checkpoint SET status='restoring' WHERE id=?", (ref,)
        )
        if row["before"] is None:
            path.unlink()
        else:
            fd, tmp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(row["before"])
                os.chmod(tmp, row["mode"])
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        store.db.execute(
            "UPDATE file_checkpoint SET status='restored' WHERE id=?", (ref,)
        )
        store.db.execute(
            "UPDATE observation SET valid=0 WHERE path=? AND sid IN (SELECT id FROM session WHERE wid=?)",
            (row["path"], store.wid),
        )
    return "已恢复 " + row["path"]
