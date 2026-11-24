"""Construct an opt-in OS-isolated shell. Unsupported backends fail closed."""

import os
import shutil
import sys
from pathlib import Path


def isolated(root: Path, start: Path, command: str, scratch: Path):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "LANG": "C",
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
    }
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        profile = """(version 1)
(deny default)
(allow process-fork process-exec sysctl-read)
(allow file-read-metadata)
(allow file-read* (subpath "/bin") (subpath "/usr") (subpath "/System") (subpath "/Library/Developer") (subpath "/dev") (subpath (param "ROOT")) (subpath (param "TMP")))
(allow file-write* (subpath (param "ROOT")) (subpath (param "TMP")) (literal "/dev/null"))
(deny file-write* (subpath (param "GIT")))
"""
        return [
            "/usr/bin/sandbox-exec",
            "-D",
            "ROOT=" + str(root),
            "-D",
            "TMP=" + str(scratch.resolve()),
            "-D",
            "GIT=" + str(root / ".git"),
            "-p",
            profile,
            "/bin/sh",
            "-c",
            command,
        ], env
    if sys.platform.startswith("linux") and (
        bwrap := shutil.which("bwrap", path=os.defpath)
    ):
        argv = [
            bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--new-session",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for directory in ["/usr", "/bin", "/lib", "/lib64"]:
            if os.path.exists(directory):
                argv.extend(["--ro-bind", directory, directory])
        argv.extend(["--bind", str(root), "/work"])
        if (root / ".git").exists():
            argv.extend(["--ro-bind", str(root / ".git"), "/work/.git"])
        argv.extend(
            [
                "--chdir",
                str(Path("/work") / start.relative_to(root)),
                "/bin/sh",
                "-c",
                command,
            ]
        )
        env.update(HOME="/tmp", TMPDIR="/tmp")
        return argv, env
    raise RuntimeError("当前系统没有可用的命令隔离后端")
