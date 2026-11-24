"""User operations shared by interactive and non-interactive frontends."""

from __future__ import annotations

import asyncio
import json
import re
import shlex

from filelock import FileLock

from .agent import Agent, probe
from .checkpoints import edit_id, restore
from .context import Builder, encoded
from .extensions import Server, connections, fingerprint, mcp_schema, servers, skills
from .runtime import Hook
from .state import Profile, Store, digest, git_environment, uid
from .tools import REGISTRY, Approve, Create, Edit, Notify, Toolbox
from .verification import workspace_checks

HELP = """/symbols [QUERY] · /sandbox on|off · /trace [full|off|BEFORE_ID] · /metrics · /checks · /hooks · /policy ask|auto|readonly · /role ROLE MODEL · /runtime FIELD VALUE
/fork · /worktree NAME · /checkpoints · /rollback ID · /new · /sessions · /resume ID · /continue · /history [event ID] · /diff
/model [use|delete NAME] · /model add（表单）
/plan [on|execute|off] · /agents [on|parallel|off] · /context · /compact
/memory [add SCOPE TAG TEXT | edit ID TEXT | delete ID | copy ID]
/skills [use NAME|off] · /mcp [add NAME COMMAND ARGS…|trust NAME|tools|stop NAME]
/permissions [trust|untrust|reset|read ask|read allow] · /files PATTERN · /help
@path 或 @path:起始行-结束行 引用文件。作用域 session、project、global；always 标签始终相关。
Enter 发送，Shift+Enter 或 Ctrl+J 换行，Ctrl+C 停止，Ctrl+Q 退出。"""


class Service:
    def __init__(self, s: Store, approve: Approve, notify: Notify, sid: str = ""):
        self.s = s
        self.approve = approve
        self.notify = notify
        self.sid = s.session(sid)["id"] if sid else s.new()

    @property
    def box(self):
        return Toolbox(self.s, self.sid, self.approve, self.notify)

    async def save_profile(self, p: Profile, secret: str | None = None):
        if secret and p.env:
            raise ValueError("API Key 和环境变量名请只填一项")
        await probe(p, self.s, secret)
        if secret:
            p = p.model_copy(update={"credential": self.s.save_key(secret)})
        self.s.store_profile(p)

    async def submit(self, text: str) -> str:
        continuing = text.strip() == "/continue"
        planned = text.strip() == "/plan execute"
        if planned and self.s.session(self.sid)["plan"] == "[]":
            raise ValueError("请先在 /plan on 中制定计划")
        if text.startswith("/") and not (continuing or planned):
            return await self.command(text)
        if not text.strip():
            raise ValueError("需求为空")
        with FileLock(str(self.s.home / (self.sid + ".run.lock")), timeout=0):
            unknown = self.s.db.execute(
                """SELECT name FROM checkpoint c JOIN run r ON r.id=c.rid WHERE sid=? AND c.status IN ('pending','unknown')""",
                (self.sid,),
            ).fetchall()
            if unknown:
                raise ValueError(
                    "上次操作结果未确认。先 /resume 核对，再 /ack 确认已检查磁盘与外部状态"
                )
            if continuing:
                row = self.s.db.execute(
                    "SELECT r.id,e.body FROM run r JOIN event e ON e.rid=r.id AND e.kind='user' WHERE r.sid=? AND r.id=(SELECT id FROM run WHERE sid=? ORDER BY rowid DESC LIMIT 1) AND r.status IN ('interrupted','cancelled')",
                    (self.sid, self.sid),
                ).fetchone()
                if not row:
                    raise ValueError("本会话最近一轮没有可继续的中断任务")
                self.s.observations(self.sid)
                ref_rows = self.s.db.execute(
                    "SELECT body FROM event WHERE rid=? AND kind='attachment' ORDER BY id",
                    (row["id"],),
                )
                refs = [json.loads(entry[0]) for entry in ref_rows]
                return await Agent(self.s, self.sid, self.approve, self.notify).execute(
                    json.loads(row["body"]),
                    row["id"],
                    attachments=await self.attachments(refs),
                )
            refs = []
            for match in re.finditer(
                r'(?:^|\s)@(?:"([^"]+)"|([^\s:，。；！？]+))(?::(\d+)(?:-(\d+))?)?',
                text,
            ):
                first = int(match[3] or 1)
                last = int(match[4] or (first + 99))
                refs.append(
                    {
                        "path": match[1] or match[2],
                        "start": first,
                        "count": last - first + 1,
                    }
                )
            old = self.s.session(self.sid)["snapshot"]
            new = self.s.snapshot()
            self.s.observations(self.sid, json.loads(old)[:2] != json.loads(new)[:2])
            return await Agent(self.s, self.sid, self.approve, self.notify).execute(
                text,
                attachments=await self.attachments(refs),
                planned=planned,
            )

    async def attachments(self, refs):
        attachments = []
        for ref in refs:
            args = {k: ref[k] for k in ("path", "start", "count")}
            stored = ref.get("version")
            try:
                result = await self.box.call("read_file", args)
            except (OSError, ValueError, PermissionError):
                if not stored:
                    raise
                result = ""
            version, _, excerpt = result.partition("\n")
            version = version.removeprefix("version=")
            stale = bool(stored and version != stored)
            attachments.append(
                {
                    **args,
                    "version": stored or version,
                    "content": "" if stale else excerpt,
                    "stale": stale,
                }
            )
        return attachments

    def resume(self, sid: str) -> str:
        s = self.s
        row = s.session(sid)
        current = s.snapshot()
        lines = [f"会话 {sid}，文件版本以磁盘为准。"]
        with FileLock(str(s.home / (sid + ".run.lock")), timeout=0):
            changed = s.observations(
                sid, json.loads(current)[:2] != json.loads(row["snapshot"])[:2]
            )
            if changed:
                lines.append("旧文件观察已失效：" + "、".join(changed))
            if current != row["snapshot"]:
                lines.extend(["Git 状态已变化", row["snapshot"], current])
            for item in s.db.execute(
                "SELECT c.* FROM checkpoint c JOIN run r ON r.id=c.rid WHERE r.sid=? AND c.status='pending'",
                (sid,),
            ).fetchall():
                info = json.loads(item["args"])
                matched = False
                if "expected" in info:
                    f = (
                        self.box.path(str(s.root / info["path"]))
                        if "path" in info
                        else self.box.path(info["arguments"]["path"])
                    )
                    matched = f.is_file() and digest(f.read_bytes()) == info["expected"]
                if matched:
                    s.db.execute(
                        "UPDATE file_checkpoint SET status='applied' WHERE id=? AND sid=? AND status='pending' AND after_hash=?",
                        (edit_id(item["rid"], item["call_id"]), sid, info["expected"]),
                    )
                name = item["name"]
                state = "reconciled" if matched else "unknown"
                note = f"{name} {item['call_id']} {state}"
                if name == "delegate" and info.get("delegation_stage"):
                    child = s.db.execute(
                        "SELECT status,answer FROM task_stage WHERE rid=? AND stage=?",
                        (item["rid"], info["delegation_stage"]),
                    ).fetchone()
                    if child and child["status"] == "done":
                        state, note = "done", child["answer"]
                    else:
                        state, note = (
                            "retryable",
                            "委派未完成；恢复同一任务时可从子阶段继续。",
                        )
                elif name in REGISTRY and REGISTRY[name][1] == "read":
                    state, note = "retryable", "只读调用被中断，可按需重新读取。"
                s.db.execute(
                    "UPDATE checkpoint SET status=?,result=? WHERE rid=? AND call_id=?",
                    (state, note, item["rid"], item["call_id"]),
                )
                s.log(
                    sid,
                    item["rid"],
                    "result",
                    {"name": item["name"], "text": note, "call_id": item["call_id"]},
                )
                lines.append(note)
            s.db.execute(
                "UPDATE run SET status='interrupted' WHERE sid=? AND status='running'",
                (sid,),
            )
            s.db.execute("UPDATE session SET snapshot=? WHERE id=?", (current, sid))
            self.sid = sid
            for e in s.db.execute(
                "SELECT kind,body FROM event WHERE sid=? ORDER BY id DESC LIMIT 3",
                (sid,),
            ).fetchall()[::-1]:
                lines.append(e["kind"] + " " + e["body"][:350])
        return "\n".join(lines)

    async def command(self, text: str) -> str:
        a = shlex.split(text)
        cmd = a.pop(0)
        s = self.s
        db = s.db
        if s.get(self.sid, "policy") == "readonly" and (
            cmd in {"/worktree", "/rollback"}
            or cmd == "/mcp"
            and a
            and a[0] in {"add", "trust", "tools"}
        ):
            raise PermissionError("当前会话只读")
        if cmd == "/policy" and a in ([], ["ask"], ["auto"], ["readonly"]):
            if a:
                s.put(self.sid, "policy", a[0])
            return s.get(self.sid, "policy", "ask")
        if (
            cmd == "/role"
            and len(a) == 2
            and a[0] in {"Explorer", "Implementer", "Reviewer"}
        ):
            if not s.find_profile(a[1]):
                raise ValueError("模型配置不存在")
            s.put(self.sid, "role:" + a[0], a[1])
            return a[1]
        if cmd == "/hooks":
            hooks = json.loads(s.get(self.sid, "hooks", "[]"))
            if a == ["clear"]:
                hooks = []
            elif len(a) >= 3:
                hook = Hook(event=a[0], tool=a[1], action=a[2], message=" ".join(a[3:]))
                if hook.event == "PostToolUse" and hook.action == "deny":
                    raise ValueError("执行后只能记录，无法拒绝已执行的操作")
                hooks.append(hook.model_dump())
            elif a:
                raise ValueError(
                    "/hooks PreToolUse|PostToolUse 工具模式 trace|deny [说明]"
                )
            s.put(
                self.sid,
                "hooks",
                s.clean(encoded(hooks))
                if not hooks
                else encoded([{**h, "message": s.clean(h["message"])} for h in hooks]),
            )
            return encoded(hooks)
        if cmd == "/sandbox" and a in ([], ["on"], ["off"]):
            if a:
                s.put(self.sid, "sandbox", a[0])
            return s.get(self.sid, "sandbox", "off")
        if cmd == "/runtime":
            configured = s.profile()
            fields = {
                "fallback",
                "allowed_tools",
                "retries",
                "rpm",
                "token_limit",
                "input_price",
                "output_price",
                "cost_limit",
                "stream_usage",
                "prompt_cache",
                "repair_rounds",
                "verification_baseline",
            }
            if len(a) == 2 and a[0] in fields:
                if a[0] == "fallback" and a[1] and not s.find_profile(a[1]):
                    raise ValueError("备用模型不存在")
                value = (
                    (None if a[1] == "*" else a[1].split(","))
                    if a[0] == "allowed_tools"
                    else a[1]
                )
                configured = Profile.model_validate(
                    {**configured.model_dump(), a[0]: value}
                )
                s.store_profile(configured)
            elif a:
                raise ValueError("/runtime FIELD VALUE")
            return encoded(configured.model_dump(include=fields))
        if cmd == "/symbols":
            return await self.box.call("repo_map", {"query": " ".join(a)})
        if cmd == "/checks":
            return encoded(workspace_checks(s.start, s.root))
        if cmd == "/trace":
            if a in (["full"], ["off"]):
                s.put(self.sid, "trace_full", "on" if a == ["full"] else "off")
                return "完整请求记录 " + s.get(self.sid, "trace_full")
            before = int(a[0]) if len(a) == 1 else 2**63 - 1
            return encoded(
                [
                    {
                        "id": r["id"],
                        "run": r["rid"],
                        "event": r["kind"],
                        "data": json.loads(r["body"]),
                    }
                    for r in db.execute(
                        "SELECT * FROM event WHERE sid=? AND id<? ORDER BY id DESC LIMIT 200",
                        (self.sid, before),
                    )
                ][::-1]
            )
        if cmd == "/metrics":
            counts = dict(
                db.execute(
                    "SELECT status,count(*) FROM run WHERE sid=? GROUP BY status",
                    (self.sid,),
                )
            )
            samples = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM event WHERE sid=? AND kind='usage'", (self.sid,)
                )
            ]
            reported = [r for r in samples if r["cached"] is not None]
            total = sum(r["input"] for r in reported)
            return encoded(
                {
                    "runs": counts,
                    "success_rate": sum(
                        counts.get(status, 0)
                        for status in ("complete", "answered", "verified")
                    )
                    / max(
                        1,
                        sum(
                            v
                            for k, v in counts.items()
                            if k not in {"running", "forked"}
                        ),
                    ),
                    "tokens": sum(r["tokens"] for r in samples),
                    "cache_hit_rate": sum(bool(r["cached"]) for r in reported)
                    / len(reported)
                    if reported
                    else None,
                    "estimated_requests": sum(r["estimated"] for r in samples),
                    "cost": sum(r["cost"] for r in samples)
                    if samples and all(r.get("priced") for r in samples)
                    else None,
                    "unsettled_requests": db.execute(
                        "SELECT count(*) FROM reservation WHERE rid IN (SELECT id FROM run WHERE sid=?)",
                        (self.sid,),
                    ).fetchone()[0],
                    "cached_input_ratio": sum(r["cached"] for r in reported) / total
                    if total
                    else None,
                }
            )
        if cmd == "/help":
            return HELP
        if cmd == "/new":
            self.sid = s.new()
            return self.sid
        if cmd == "/fork" and not a:
            source = self.sid
            db.execute("SAVEPOINT fork")
            try:
                child = s.new()
                db.execute(
                    "UPDATE session SET plan=(SELECT plan FROM session WHERE id=?), mode=(SELECT mode FROM session WHERE id=?) WHERE id=?",
                    (source, source, child),
                )
                for (parent_run,) in db.execute(
                    "SELECT id FROM run WHERE sid=? ORDER BY rowid", (source,)
                ).fetchall():
                    rid = uid()
                    db.execute("INSERT INTO run VALUES(?,?,?)", (rid, child, "forked"))
                    db.execute(
                        """INSERT INTO event(sid,rid,kind,body) SELECT ?,?,kind,body FROM event
                        WHERE sid=? AND rid=? AND kind IN ('user','assistant','result','verification','role') ORDER BY id""",
                        (child, rid, source, parent_run),
                    )
                    db.execute(
                        """INSERT INTO checkpoint SELECT ?,call_id,name,args,status,result FROM checkpoint
                        WHERE rid=? AND status='done'""",
                        (rid, parent_run),
                    )
                    db.execute(
                        """INSERT INTO observation SELECT ?,?,call_id,path,version,valid FROM observation WHERE sid=? AND rid=?""",
                        (child, rid, source, parent_run),
                    )
                for key in ("policy", "read", "sandbox"):
                    if value := s.get(source, key):
                        s.put(child, key, value)
                for m in s.memories(source):
                    if m["scope"] == "session":
                        s.remember(
                            child, "session", m["body"], m["tag"], "fork:" + source
                        )
                db.execute(
                    "INSERT INTO retired SELECT scope,?,body FROM retired WHERE scope='session' AND owner=?",
                    (child, source),
                )
                db.execute("RELEASE fork")
            except BaseException:
                db.execute("ROLLBACK TO fork")
                db.execute("RELEASE fork")
                raise
            self.sid = child
            return child
        if cmd == "/checkpoints" and not a:
            return encoded(
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,path,status FROM file_checkpoint WHERE sid=? ORDER BY rowid DESC",
                        (self.sid,),
                    )
                ]
            )
        if cmd == "/rollback" and len(a) == 1:
            row = db.execute(
                "SELECT path FROM file_checkpoint WHERE sid=? AND id=?",
                (self.sid, a[0]),
            ).fetchone()
            if not row:
                raise ValueError("找不到检查点")
            if (
                s.session(self.sid)["mode"] == "plan"
                or s.get(self.sid, "policy") == "readonly"
                or await self.approve("edit", str(s.root / row[0]))
                not in {"once", "session"}
            ):
                raise PermissionError("未授权恢复文件")
            return restore(self.box, a[0])
        if cmd == "/worktree" and len(a) == 1:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a[0]):
                raise ValueError("工作树名称只接受字母、数字、下划线和短横线")
            target = s.home / "worktrees" / s.wid / a[0]
            argv = [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(s.root),
                "worktree",
                "add",
                "--detach",
                str(target),
                "HEAD",
            ]
            if (
                s.session(self.sid)["mode"] == "plan"
                or s.get(self.sid, "policy") == "readonly"
                or await self.approve("command", shlex.join(argv))
                not in {"once", "session"}
            ):
                raise PermissionError("未授权创建工作树")
            target.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=git_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, error = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode:
                raise ValueError(s.clean(error.decode(errors="replace")))
            return str(target)
        if cmd == "/sessions":
            return "\n".join(
                f"{r['id']}  {r['created']}  {r['mode']}"
                for r in db.execute(
                    "SELECT * FROM session WHERE wid=? ORDER BY rowid DESC", (s.wid,)
                )
            )
        if cmd == "/resume" and len(a) <= 1:
            return self.resume(a[0] if a else self.sid)
        if cmd == "/ack" and not a:
            if (
                await self.approve("recovery", "确认已核对未知操作，可继续提交新需求")
                != "once"
            ):
                raise PermissionError("尚未确认")
            db.execute(
                "UPDATE checkpoint SET status='acknowledged' WHERE status='unknown' AND rid IN (SELECT id FROM run WHERE sid=?)",
                (self.sid,),
            )
            return "已记录核对"
        if cmd == "/history":
            rows = db.execute(
                "SELECT id,kind,body FROM event WHERE sid=? AND (?='' OR id=?) ORDER BY id",
                (self.sid, a[0] if a else "", a[0] if a else 0),
            )
            return "\n".join(
                f"{r['id']} {r['kind']} {r['body'] if a else r['body'][:350]}"
                for r in rows
            )
        if cmd == "/diff":
            diffs = [
                r[0]
                for r in db.execute(
                    "SELECT result FROM checkpoint WHERE status='done' AND name IN ('create_file','edit_file','apply_patch') AND rid IN (SELECT id FROM run WHERE sid=?) ORDER BY rowid",
                    (self.sid,),
                )
            ]
            paths = [
                r[0]
                for r in db.execute(
                    "SELECT body FROM event WHERE sid=? AND kind='changed_paths' ORDER BY id",
                    (self.sid,),
                )
            ]
            return (
                self.box.diff()
                + "\n会话编辑记录\n"
                + "\n".join(diffs)
                + ("\n命令期间检测到变化\n" + "\n".join(paths) if paths else "")
            )
        if cmd == "/model":
            if not a:
                return (
                    "\n".join(r[0] for r in db.execute("SELECT name FROM profile"))
                    + "\n当前 "
                    + s.get("*", "model")
                    + "\n/model add · /model use NAME · /model delete NAME"
                )
            if (
                len(a) == 2
                and db.execute("SELECT 1 FROM profile WHERE name=?", (a[1],)).fetchone()
            ):
                if a[0] == "use":
                    s.put("*", "model", a[1])
                    return a[1]
                if a[0] == "delete":
                    profile = s.find_profile(a[1])
                    db.execute("DELETE FROM profile WHERE name=?", (a[1],))
                    if profile:
                        s.discard_key(profile.credential)
                    if s.get("*", "model") == a[1]:
                        s.put("*", "model", "")
                    return "已删除 " + a[1]
        if cmd in {"/plan", "/agents"}:
            role = {"/plan": "plan", "/agents": "agents"}[cmd]
            if cmd == "/agents" and a in (["parallel"], ["on"]):
                s.put(self.sid, "parallel", "yes" if a == ["parallel"] else "no")
                a = ["on"]
            if a in (["on"], ["off"], ["execute"]):
                db.execute(
                    "UPDATE session SET mode=? WHERE id=?",
                    (role if a == ["on"] else "develop", self.sid),
                )
            row = s.session(self.sid)
            return f"{row['mode']}\n{row['plan']}"
        if cmd == "/context":
            return s.get(self.sid, "context", "尚未调用模型")
        if cmd == "/compact":
            return encoded(Builder(s, self.sid).compact())
        if cmd == "/memory":
            return await self.memory(a)
        if cmd == "/skills":
            catalog = skills(s)
            if a == ["off"]:
                s.put(self.sid, "skill", "")
            elif len(a) == 2 and a[0] == "use" and a[1] in catalog:
                s.put(self.sid, "skill", a[1])
            last = db.execute(
                "SELECT id FROM run WHERE sid=? ORDER BY rowid DESC LIMIT 1",
                (self.sid,),
            ).fetchone()
            return encoded(
                {
                    "active": s.get(self.sid, "skill"),
                    "last_run": s.get(last[0], "skill") if last else "",
                    "catalog": catalog,
                }
            )
        if cmd == "/mcp":
            cfg = servers(s)
            if a == ["tools"]:
                if s.session(self.sid)["mode"] == "plan":
                    raise PermissionError("只读计划不启动外部工具")
                async with connections(s, self.notify) as active:
                    return encoded(mcp_schema(active))
            if len(a) >= 3 and a[0] == "add":
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", a[1]) or s.clean(
                    shlex.join(a[2:])
                ) != shlex.join(a[2:]):
                    raise ValueError("服务名称无效或命令含凭据")
                path = self.box.path(str(s.root / ".luban/mcp.json"))
                cfg[a[1]] = Server(command=a[2], args=a[3:])
                body = (
                    json.dumps(
                        {k: v.model_dump() for k, v in cfg.items()},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                if path.exists():
                    original = path.read_bytes()
                    self.box.write(
                        path,
                        Edit(
                            path=str(path),
                            old=original.decode(),
                            new=body,
                            version=digest(original),
                        ),
                    )
                else:
                    self.box.write(path, Create(path=str(path), content=body))
            if len(a) == 2 and a[1] in cfg:
                server = cfg[a[1]]
                if a[0] == "stop":
                    s.put(s.wid, "mcp:" + a[1], "")
                elif a[0] == "trust":
                    detail = f"将此启动命令保存为当前工作区信任：{s.root}/.luban/mcp.json · {shlex.join([server.command, *server.args])}"
                    if await self.approve("启动 MCP", detail) in {"once", "session"}:
                        s.put(s.wid, "mcp:" + a[1], fingerprint(server))
            return encoded(
                {
                    k: {
                        **v.model_dump(),
                        "trusted": s.get(s.wid, "mcp:" + k) == fingerprint(v),
                    }
                    for k, v in cfg.items()
                }
            )
        if cmd == "/permissions":
            if a in (["read", "ask"], ["read", "allow"]):
                s.put(self.sid, "read", a[1])
            if a == ["reset"]:
                db.execute(
                    "DELETE FROM setting WHERE owner=? AND key LIKE 'grant:%'",
                    (self.sid,),
                )
            if a in (["trust"], ["untrust"]):
                s.put(s.wid, "trust", "yes" if a == ["trust"] else "no")
            grants = [
                r[0]
                for r in db.execute(
                    "SELECT key FROM setting WHERE owner=? AND key LIKE 'grant:%'",
                    (self.sid,),
                )
            ]
            return encoded(
                {
                    "read": str(s.root),
                    "policy": s.get(self.sid, "policy", "ask"),
                    "sandbox": s.get(self.sid, "sandbox", "off"),
                    "read_policy": s.get(self.sid, "read", "allow"),
                    "instructions_trusted": s.get(s.wid, "trust") == "yes",
                    "grants": grants,
                    "host_shell": "隔离后端必需"
                    if s.get(self.sid, "sandbox") == "on"
                    else "命令可访问当前系统用户权限范围内的文件和网络",
                }
            )
        if cmd == "/files":
            return await self.box.call(
                "match_files", {"pattern": "*" + (a[0] if a else "") + "*"}
            )
        raise ValueError("命令或参数无效，见 /help")

    async def memory(self, a: list[str]) -> str:
        s = self.s
        rows = s.memories(self.sid)
        if not a:
            return encoded(
                {
                    "records": rows,
                    "last_loaded": json.loads(s.get(self.sid, "context", "{}")).get(
                        "memories", []
                    ),
                }
            )
        if len(a) >= 4 and a[0] == "add" and a[1] in {"global", "project", "session"}:
            return s.remember(self.sid, a[1], " ".join(a[3:]), a[2])
        if len(a) == 2 and a[0] == "copy":
            row = s.db.execute(
                "SELECT m.*,w.path FROM memory m JOIN workspace w ON w.id=m.owner WHERE m.id=? AND scope='project' AND active=1",
                (a[1],),
            ).fetchone()
            if not row:
                raise ValueError("找不到指定的项目记忆")
            if await self.approve(
                "copy", f"{row['path']} → {s.root}\n{row['body']}"
            ) in {"once", "session"}:
                return s.remember(
                    self.sid, "project", row["body"], row["tag"], "copy:" + row["id"]
                )
            raise PermissionError("未确认复制")
        if len(a) >= 2 and any(m["id"] == a[1] for m in rows):
            if a[0] == "delete":
                s.db.execute(
                    "UPDATE memory SET active=0,updated=CURRENT_TIMESTAMP WHERE id=?",
                    (a[1],),
                )
                return "已删除"
            if a[0] == "edit" and len(a) > 2:
                body = " ".join(a[2:])
                if s.clean(body) != body:
                    raise ValueError("记忆不能包含凭据")
                s.db.execute(
                    "UPDATE memory SET body=?,updated=CURRENT_TIMESTAMP WHERE id=?",
                    (body, a[1]),
                )
                return "已更新"
        raise ValueError("记忆操作无效，见 /help")
