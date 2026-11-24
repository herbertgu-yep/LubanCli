"""Workspace identity, local event journal, preferences and model profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, model_validator


def uid() -> str:
    return uuid.uuid4().hex


def git_environment() -> dict[str, str]:
    return {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *args,
        ],
        env=git_environment(),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return process.stdout.rstrip("\n") if process.returncode == 0 else ""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    base_url: str
    model: str = Field(min_length=1)
    window: int = Field(default=32768, ge=512)
    output: int = Field(default=2048, ge=32)
    env: str = Field(default="", pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    credential: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    setup_required: bool = False
    tokenizer: str = ""
    tools: bool = True
    calls: int = Field(default=40, ge=1, le=500)
    retries: int = Field(default=2, ge=0, le=5)
    fallback: str = ""
    allowed_tools: list[str] | None = None
    rpm: int = Field(default=0, ge=0)
    token_limit: int = Field(default=0, ge=0)
    input_price: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_price: float = Field(default=0, ge=0, allow_inf_nan=False)
    cost_limit: float = Field(default=0, ge=0, allow_inf_nan=False)
    stream_usage: bool = False
    prompt_cache: bool = False
    verification_baseline: bool = False
    repair_rounds: int = Field(default=1, ge=0, le=4)
    seconds: int = Field(default=600, ge=1)
    output_bytes: int = Field(default=2000000, ge=1024)

    @model_validator(mode="after")
    def validate_endpoint(self) -> Profile:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Base URL 需要 HTTP(S) 地址，请在 API Key 栏中填写密钥")
        if self.output >= self.window:
            raise ValueError("预留输出必须小于上下文窗口")
        return self

    def key(self, home: Path) -> str:
        if self.env:
            if not os.environ.get(self.env):
                raise ValueError(f"请设置环境变量 {self.env}")
            return os.environ[self.env]
        if self.setup_required:
            raise ValueError("请打开 /model edit，重新填写 API Key，或留空测试免密端点")
        if not self.credential:
            return "local-no-key"
        try:
            return json.loads(
                (home / "credentials" / (self.credential + ".json")).read_text()
            )["api_key"]
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(
                "本地 API Key 文件不可用，请用 /model edit 重新填写"
            ) from exc


class Store:
    def __init__(self, start: Path, home: Path | None = None):
        self.start = start.resolve(strict=True)
        self.root = Path(
            git(self.start, "rev-parse", "--show-toplevel") or self.start
        ).resolve()
        self.wid = digest(os.fsencode(self.root))
        self.home = home or (
            Path(os.environ["LUBAN_DATA_DIR"])
            if os.environ.get("LUBAN_DATA_DIR")
            else user_data_path("LubanCli", appauthor=False)
        )
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        db_path = self.home / "state.sqlite3"
        if not db_path.exists():
            db_path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.secrets: set[str] = set()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS workspace(id PRIMARY KEY,path);
        CREATE TABLE IF NOT EXISTS session(id PRIMARY KEY,wid,created DEFAULT CURRENT_TIMESTAMP,snapshot,mode DEFAULT 'develop',plan DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS run(id PRIMARY KEY,sid,status);
        CREATE TABLE IF NOT EXISTS task_stage(rid,stage,status,answer,review,PRIMARY KEY(rid,stage));
        CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,sid,rid,kind,body);
        CREATE INDEX IF NOT EXISTS history ON event(sid,id);
        CREATE TABLE IF NOT EXISTS memory(id PRIMARY KEY,scope,owner,body,tag,source,created DEFAULT CURRENT_TIMESTAMP,updated DEFAULT CURRENT_TIMESTAMP,active DEFAULT 1,kind TEXT);
        CREATE TABLE IF NOT EXISTS retired(scope,owner,body);
        CREATE TRIGGER IF NOT EXISTS retire_memory AFTER UPDATE ON memory
        WHEN (OLD.body<>NEW.body OR (OLD.active=1 AND NEW.active=0))
        BEGIN INSERT INTO retired VALUES(OLD.scope,OLD.owner,OLD.body); END;
        CREATE TABLE IF NOT EXISTS summary(sid PRIMARY KEY,until_id,body);
        CREATE TABLE IF NOT EXISTS profile(name PRIMARY KEY,body);
        CREATE TABLE IF NOT EXISTS setting(owner,key,value,PRIMARY KEY(owner,key));
        CREATE TABLE IF NOT EXISTS observation(sid,rid,call_id,path,version,valid DEFAULT 1,PRIMARY KEY(rid,call_id));
        CREATE TABLE IF NOT EXISTS reservation(id PRIMARY KEY,rid,role,tokens,cost);
        CREATE TABLE IF NOT EXISTS file_checkpoint(id PRIMARY KEY,sid,path,before BLOB,mode,after_hash,after_mode,status);
        CREATE TABLE IF NOT EXISTS checkpoint(rid,call_id,name,args,status,result,PRIMARY KEY(rid,call_id));
        """)
        if "kind" not in {
            r["name"] for r in self.db.execute("PRAGMA table_info(memory)")
        }:
            self.db.execute("ALTER TABLE memory ADD COLUMN kind TEXT")
            self.db.execute(
                "UPDATE memory SET kind=CASE scope WHEN 'global' THEN 'preference' WHEN 'project' THEN 'convention' ELSE 'constraint' END"
            )
        self.db.execute(
            "INSERT OR IGNORE INTO workspace VALUES(?,?)", (self.wid, str(self.root))
        )

    def clean(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)(Bearer\s+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[\"']?)[^\s\"',}]+",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(
            r'(?i)("(?:api[_-]?key|token|password|secret)"\s*:\s*")[^"]+',
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
            "[REDACTED]",
            value,
        )
        return re.sub(r"\bsk-[\w-]{12,}", "[REDACTED]", value)

    def observations(self, sid: str, invalidate: bool = False) -> list[str]:
        """Reconcile the latest observed file versions against this worktree."""
        changed = []
        for row in self.db.execute(
            "SELECT * FROM observation WHERE sid=? AND valid=1", (sid,)
        ).fetchall():
            p = (self.root / row["path"]).resolve()
            try:
                equal = (
                    not invalidate
                    and p.is_relative_to(self.root)
                    and p.is_file()
                    and p.stat().st_size <= 64 * 1024 * 1024
                    and digest(p.read_bytes()) == row["version"]
                )
            except OSError:
                equal = False
            if not equal:
                changed.append(row["path"])
                self.db.execute(
                    "UPDATE observation SET valid=0 WHERE rid=? AND call_id=?",
                    (row["rid"], row["call_id"]),
                )
        if changed or invalidate:
            latest = self.db.execute(
                "SELECT COALESCE(MAX(id),0) FROM event WHERE sid=?", (sid,)
            ).fetchone()[0]
            self.put(sid, "evidence_epoch", str(latest))
        return changed

    def snapshot(self) -> str:
        return json.dumps(
            [
                git(self.root, "branch", "--show-current"),
                git(self.root, "rev-parse", "HEAD"),
                git(self.root, "status", "--short"),
            ]
        )

    def new(self) -> str:
        sid = uid()
        self.db.execute(
            "INSERT INTO session(id,wid,snapshot) VALUES(?,?,?)",
            (sid, self.wid, self.snapshot()),
        )
        return sid

    def session(self, sid: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM session WHERE wid=? AND id=?", (self.wid, sid)
        ).fetchone()
        if row is None:
            raise ValueError("当前工作区没有这个会话")
        return row

    def log(self, sid: str, rid: str, kind: str, body: Any) -> int:

        # Redact leaf strings before encoding so escaping never corrupts stored JSON.
        def scrub(obj: Any) -> Any:
            if isinstance(obj, str):
                return self.clean(obj)
            if isinstance(obj, list):
                return [scrub(x) for x in obj]
            if isinstance(obj, dict):
                return {k: scrub(v) for k, v in obj.items()}
            return obj

        encoded = json.dumps(scrub(body), ensure_ascii=False)
        cur = self.db.execute(
            "INSERT INTO event(sid,rid,kind,body) VALUES(?,?,?,?)",
            (sid, rid, kind, encoded),
        )
        return int(cur.lastrowid or 0)

    def get(self, owner: str, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM setting WHERE owner=? AND key=?", (owner, key)
        ).fetchone()
        return row[0] if row else default

    def put(self, owner: str, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO setting VALUES(?,?,?)", (owner, key, value)
        )

    def memories(self, sid: str) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM memory WHERE active=1 AND ((scope='global' AND owner='*') OR (scope='project' AND owner=?) OR (scope='session' AND owner=?)) ORDER BY rowid DESC",
                (self.wid, sid),
            )
        ]

    def remember(
        self, sid: str, scope: str, body: str, tag: str = "", source: str = "user"
    ) -> str:
        owner = {"global": "*", "project": self.wid, "session": sid}[scope]
        if scope == "global" and str(self.root) in body:
            raise ValueError("项目路径只能保存在项目或会话作用域")
        if self.clean(body) != body:
            raise ValueError("记忆不能包含凭据")
        key = uid()
        self.db.execute(
            "INSERT INTO memory(id,scope,owner,body,tag,source,kind) VALUES(?,?,?,?,?,?,?)",
            (
                key,
                scope,
                owner,
                body,
                tag,
                source,
                {
                    "global": "preference",
                    "project": "convention",
                    "session": "constraint",
                }[scope],
            ),
        )
        return key

    def save_key(self, secret: str) -> str:
        directory = self.home / "credentials"
        directory.mkdir(mode=0o700, exist_ok=True)
        ref = uid()
        path = directory / (ref + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"api_key": secret}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return ref

    def discard_key(self, ref: str) -> None:
        if ref:
            (self.home / "credentials" / (ref + ".json")).unlink(missing_ok=True)

    def store_profile(self, profile: Profile) -> None:
        old = self.find_profile(profile.name)
        self.db.execute(
            "INSERT OR REPLACE INTO profile VALUES(?,?)",
            (profile.name, profile.model_dump_json()),
        )
        self.put("*", "model", profile.name)
        if old and old.credential != profile.credential:
            self.discard_key(old.credential)

    def profile(self) -> Profile:
        profile = self.find_profile(self.get("*", "model"))
        if not profile:
            raise ValueError("请先通过 /model 或 Luban model-add 配置模型")
        return profile

    def find_profile(self, name: str) -> Profile | None:
        row = self.db.execute(
            "SELECT body FROM profile WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        legacy = data.pop("use_keyring", None)
        if "credential" not in data and not data.get("env"):
            data["setup_required"] = legacy is not False
        return Profile.model_validate(data)
