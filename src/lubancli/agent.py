"""Shared streaming agent, tool journal and bounded role handoffs."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, deque
from contextlib import asynccontextmanager, nullcontext
from typing import Any, Literal

from filelock import FileLock
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, BadRequestError
from pydantic import BaseModel, Field

from .context import Builder, encoded
from .extensions import (
    UnknownToolOutcome,
    available_skills,
    connections,
    invoke,
    mcp_schema,
    resource,
)
from .repository import RepositoryMap
from .runtime import Lane, Phase, Runtime
from .state import Profile, Store, digest, uid
from .tools import (
    REGISTRY,
    Approve,
    Args,
    Create,
    Edit,
    Notify,
    Patch,
    Toolbox,
    bounded,
    schema,
)
from .verification import check_fingerprint, workspace_checks


class Event(BaseModel):
    kind: str
    text: str


class Step(Args):
    goal: str
    dependencies: list[int] = []
    status: Literal["pending", "active", "done", "blocked"] = "pending"
    acceptance: str
    evidence_kind: Literal["read", "change", "check"] | None = None
    evidence: str = ""


class Plan(Args):
    steps: list[Step] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1)


class Delegation(Args):
    role: Literal["Explorer", "Implementer", "Reviewer"]
    goal: str = Field(min_length=1, max_length=2000)
    files: list[str] = Field(default_factory=list, max_length=24)
    constraints: list[str] = Field(default_factory=list, max_length=16)
    acceptance: str = Field(min_length=1, max_length=1000)


class SkillChoice(Args):
    name: str


class Resource(Args):
    path: str


class OutputRecord(Args):
    run_id: str
    call_id: str
    start: int = Field(default=0, ge=0)
    length: int = Field(default=2500, ge=1, le=8000)


class Constraint(Args):
    quote: str = Field(min_length=1, max_length=2000)


class Findings(Args):
    issues: list[str] = Field(max_length=12)
    evidence: str = Field(min_length=1)


class Memory(Args):
    scope: Literal["global", "project", "session"]
    content: str
    tag: str = ""


@asynccontextmanager
async def client_for(p: Profile, s: Store, secret: str | None = None):
    key = secret or p.key(s.home)
    if key != "local-no-key":
        s.secrets.add(key)
    async with AsyncOpenAI(
        api_key=key, base_url=p.base_url, max_retries=0, timeout=60
    ) as client:
        yield client


async def probe(p: Profile, s: Store, secret: str | None = None):
    async with client_for(p, s, secret) as client:
        opts: dict[str, Any] = {}
        if p.tools:
            opts["tools"] = [
                schema("ping", "Connectivity check", Args.model_json_schema())
            ]
            opts["tool_choice"] = {"type": "function", "function": {"name": "ping"}}
        options: dict[str, Any] = {
            "model": p.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Call ping if provided, otherwise reply OK.",
                }
            ],
            "max_tokens": min(128, p.output),
            **opts,
        }
        try:
            result = await client.chat.completions.create(**options)
        except BadRequestError:
            if not p.tools:
                raise
            options.pop("tool_choice", None)
            result = await client.chat.completions.create(**options)
        if p.tools and not any(
            getattr(getattr(t, "function", None), "name", "") == "ping"
            for t in result.choices[0].message.tool_calls or []
        ):
            raise ValueError("端点未返回要求的工具调用，可关闭 tools 使用纯问答模式")


class PlanBlocked(RuntimeError):
    pass


class IncompleteResponse(RuntimeError):
    pass


async def complete(client, p, messages, schemas, notify):
    opts = {"tools": schemas} if schemas else {}
    if p.stream_usage:
        opts["stream_options"] = {"include_usage": True}
    if p.prompt_cache:
        opts["extra_body"] = {
            "prompt_cache_key": digest(encoded([p.model, messages[0]]).encode())[:48]
        }
    stream = await client.chat.completions.create(
        model=p.model, messages=messages, stream=True, max_tokens=p.output, **opts
    )
    chunks = []
    calls: dict[int, dict] = {}
    usage = None
    finish = None
    async with stream:
        async for update in stream:
            if getattr(update, "usage", None):
                usage = update.usage.model_dump()
            if not update.choices:
                continue
            choice = update.choices[0]
            finish = choice.finish_reason or finish
            delta = choice.delta
            if delta.content:
                chunks.append(delta.content)
                await notify("delta", delta.content)
            for part in delta.tool_calls or []:
                c = calls.setdefault(
                    part.index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                c["id"] += part.id or ""
                if part.function:
                    c["function"]["name"] += part.function.name or ""
                    c["function"]["arguments"] += part.function.arguments or ""
    if finish not in {"stop", "tool_calls"} or bool(calls) != (finish == "tool_calls"):
        raise IncompleteResponse(
            f"模型响应未完整结束（{finish or '连接提前关闭'}）；未执行本次响应中的工具"
        )
    msg: dict = {"role": "assistant", "content": "".join(chunks)}
    if usage:
        msg["_usage"] = usage
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return msg


class Agent:
    def __init__(self, s: Store, sid: str, approve: Approve, notify: Notify):
        self.s = s
        self.sid = sid
        self.notify = notify

        async def permission(scope, detail):
            self.s.log(
                self.sid, self.rid, "permission", {"scope": scope, "request": detail}
            )
            decision = await approve(scope, detail)
            self.s.log(
                self.sid, self.rid, "permission", {"scope": scope, "decision": decision}
            )
            return decision

        self.approve = permission
        self.box = Toolbox(s, sid, permission, notify)
        self.builder = Builder(s, sid)
        self.rid = ""
        self.changes: list[str] = []
        self.request = ""
        self.review: Findings | None = None
        self.active_step: int | None = None
        self.cycle_replans: set[str] = set()

    def recovered_groups(
        self, rid: str, stage: str, legacy: bool = False
    ) -> list[list[dict]]:
        groups = []
        for (data,) in self.s.db.execute(
            "SELECT body FROM event WHERE sid=? AND rid=? AND kind='assistant' ORDER BY id",
            (self.sid, rid),
        ):
            message = json.loads(data)
            saved_stage = message.pop("_stage", "")
            if saved_stage != stage and not (legacy and not saved_stage):
                continue
            calls = message.get("tool_calls", [])
            if not calls:
                continue
            group = [message]
            for call in calls:
                row = self.s.db.execute(
                    "SELECT c.status,c.result,o.valid FROM checkpoint c LEFT JOIN observation o ON c.rid=o.rid AND c.call_id=o.call_id WHERE c.rid=? AND c.call_id=?",
                    (rid, call["id"]),
                ).fetchone()
                if row and row["valid"] == 0:
                    result = "磁盘已变化，请重新读取。"
                elif row:
                    result = row["status"] + " " + bounded(row["result"], 3000)
                else:
                    result = "中断前没有完成记录。"
                group.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )
            groups.append(group)
        return groups

    async def execute(
        self,
        request: str,
        resume_id: str = "",
        user_text: str | None = None,
        *,
        attachments: list[dict] | None = None,
        planned: bool = False,
    ) -> str:
        self.request = (
            user_text
            if user_text is not None
            else request.split("\n\n引用文件（工具观测）\n", 1)[0]
        )
        p = self.s.profile()
        mode = "execute-plan" if planned else self.s.session(self.sid)["mode"]
        self.rid = resume_id or uid()
        db = self.s.db
        if resume_id:
            updated = db.execute(
                "UPDATE run SET status='running' WHERE id=? AND sid=? AND status IN ('cancelled','interrupted')",
                (resume_id, self.sid),
            )
            if not updated.rowcount:
                raise ValueError("本轮状态已改变，请重新恢复")
        else:
            db.execute("INSERT INTO run VALUES(?,?,?)", (self.rid, self.sid, "running"))
        mode = self.s.get(self.rid, "mode", mode)
        self.s.put(self.rid, "mode", mode)
        parallel_roles = self.s.get(
            self.rid, "parallel", self.s.get(self.sid, "parallel", "no")
        )
        self.s.put(self.rid, "parallel", parallel_roles)
        if mode == "execute-plan" and not self.s.get(self.rid, "plan"):
            self.s.put(self.rid, "plan", self.s.session(self.sid)["plan"])

        self.runtime = Runtime(self.s, self.sid, self.rid, p)
        if p.env:
            import os

            if key := os.environ.get(p.env):
                self.s.secrets.add(key)
        self.builder.attachments = attachments or []
        if not resume_id:
            self.s.log(self.sid, self.rid, "user", request)
            for attachment in self.builder.attachments:
                self.s.log(
                    self.sid,
                    self.rid,
                    "attachment",
                    {k: v for k, v in attachment.items() if k != "content"},
                )
        elif not attachments and "\n\n引用文件（工具观测）\n" in request:
            request = self.request

        remaining = [p.calls, p.output_bytes]
        if resume_id:
            self.changes.extend(
                json.loads(row[0])["path"]
                for row in db.execute(
                    "SELECT args FROM checkpoint WHERE rid=? AND name IN ('create_file','edit_file','apply_patch') AND status IN ('done','reconciled')",
                    (resume_id,),
                )
            )
            previous = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST(result AS BLOB))),0) FROM checkpoint WHERE rid=?",
                (resume_id,),
            ).fetchone()
            previous_models = db.execute(
                "SELECT COUNT(*) FROM event WHERE rid=? AND kind='assistant'",
                (resume_id,),
            ).fetchone()[0]
            attempts = db.execute(
                "SELECT count(*) FROM event WHERE rid=? AND kind='phase' AND json_extract(body,'$.state')='request'",
                (resume_id,),
            ).fetchone()[0]
            remaining = [
                p.calls - previous[0] - max(attempts, previous_models),
                p.output_bytes - previous[1],
            ]
        status = "failed"
        started = time.time()
        clock = json.loads(self.s.get(self.rid, "clock", "{}"))
        elapsed = clock.get("elapsed", 0.0)
        if previous_start := clock.get("started"):
            last = db.execute(
                "SELECT MAX(CASE WHEN json_valid(body) THEN json_extract(body,'$.time') END) FROM event WHERE rid=? AND kind IN ('phase','tool_timing','usage')",
                (self.rid,),
            ).fetchone()[0]
            elapsed += max(0, (last or previous_start) - previous_start)
        self.s.put(self.rid, "clock", encoded({"elapsed": elapsed, "started": started}))
        try:
            if elapsed >= p.seconds:
                raise RuntimeError("运行已用完时间预算")
            async with (
                asyncio.timeout(p.seconds - elapsed),
                client_for(p, self.s) as client,
            ):
                if p.tools and self.s.get(self.sid, "read", "allow") == "allow":
                    self.builder.locations = await asyncio.to_thread(
                        RepositoryMap(self.box).query, self.request, 12
                    )
                if (
                    p.verification_baseline
                    and mode != "plan"
                    and self.s.get(self.sid, "policy") != "readonly"
                    and not self.changes
                ):
                    await self.measure_baseline(p, remaining)
                async with self.extensions(mode, p) as external:

                    async def work(role, task, stage="main"):
                        saved = db.execute(
                            "SELECT status,answer,review FROM task_stage WHERE rid=? AND stage=?",
                            (self.rid, stage),
                        ).fetchone()
                        if saved and saved["status"] == "done":
                            if saved["review"]:
                                self.review = Findings.model_validate_json(
                                    saved["review"]
                                )
                            return saved["answer"]
                        if resume_id and role == "develop" and stage == "main":
                            await self.resume_delegations(p, remaining, external)
                        chain = (
                            self.recovered_groups(
                                self.rid,
                                stage,
                                legacy=stage == "main" and mode != "agents",
                            )
                            if resume_id
                            else []
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO task_stage VALUES(?,?,?,NULL,NULL)",
                            (self.rid, stage, "running"),
                        )
                        review_start = self.review

                        await self.notify("role", role)
                        self.s.log(
                            self.sid,
                            self.rid,
                            "role",
                            {"name": role, "goal": bounded(task, 3000)},
                        )
                        guard = (
                            FileLock(
                                str(self.s.home / (self.s.wid + ".writer.lock")),
                                timeout=0,
                            )
                            if role == "Implementer"
                            else nullcontext()
                        )
                        role_model = self.s.get(self.sid, "role:" + role)
                        if role_model:
                            child = self.s.find_profile(role_model)
                            if not child or not child.tools:
                                raise ValueError("委派配置不可用或不支持工具")
                            with guard:
                                async with (
                                    asyncio.timeout(child.seconds),
                                    client_for(child, self.s) as child_client,
                                ):
                                    result = await self.loop(
                                        child_client,
                                        child,
                                        role,
                                        task,
                                        external,
                                        remaining,
                                        chain,
                                        stage,
                                    )
                        else:
                            with guard:
                                result = await self.loop(
                                    client,
                                    p,
                                    role,
                                    task,
                                    external,
                                    remaining,
                                    chain,
                                    stage,
                                )
                        review = (
                            self.review.model_dump_json()
                            if self.review is not None
                            and self.review is not review_start
                            else None
                        )
                        db.execute(
                            "UPDATE task_stage SET status='done',answer=?,review=? WHERE rid=? AND stage=?",
                            (
                                self.s.clean(result),
                                self.s.clean(review) if review else None,
                                self.rid,
                                stage,
                            ),
                        )
                        return result

                    self.delegate = work
                    if mode == "execute-plan":
                        answer = await self.execute_plan(work)
                    elif mode == "agents":
                        if not p.tools:
                            raise ValueError("多 Agent 开发需要支持工具调用的模型")
                        if parallel_roles == "yes":
                            async with asyncio.TaskGroup() as group:
                                code = group.create_task(
                                    work(
                                        "Explorer",
                                        request
                                        + "\nLocate implementation entry points.",
                                        "explore-code",
                                    )
                                )
                                tests = group.create_task(
                                    work(
                                        "Explorer",
                                        request
                                        + "\nLocate test coverage and acceptance evidence.",
                                        "explore-tests",
                                    )
                                )
                            evidence = code.result() + "\n" + tests.result()
                        else:
                            evidence = await work("Explorer", request, "explore")
                        implementation = await work(
                            "Implementer",
                            request
                            + "\nExplorer evidence:\n"
                            + bounded(evidence, 4000),
                            "implement",
                        )
                        recorded_edits = [
                            r[0]
                            for r in db.execute(
                                "SELECT result FROM checkpoint WHERE rid=? AND name IN ('create_file','edit_file','apply_patch') AND status='done' ORDER BY rowid",
                                (self.rid,),
                            )
                        ]
                        evidence = (
                            self.box.diff()
                            + "\nRecorded edits:\n"
                            + "\n".join(recorded_edits)
                        )
                        verified = [
                            r[0]
                            for r in db.execute(
                                "SELECT result FROM checkpoint WHERE rid=? AND name='command' AND status='done'",
                                (self.rid,),
                            )
                        ]
                        review = await work(
                            "Reviewer",
                            request
                            + "\nInspect actual diff. Review this report:\n"
                            + bounded(implementation, 3000)
                            + "\nActual diff and command evidence:\n"
                            + bounded(evidence, 5000)
                            + "\n"
                            + bounded("\n".join(verified), 2200),
                            "review",
                        )
                        if self.review and self.review.issues:
                            implementation = await work(
                                "Implementer",
                                request
                                + "\nResolve or explain each reviewed issue:\n"
                                + self.review.model_dump_json(),
                                "review-fix",
                            )
                        answer = implementation + "\n\nReviewer\n" + review
                    else:
                        answer = await work(mode, request)
                    if (
                        self.changes
                        and mode != "plan"
                        and self.s.get(self.sid, "policy") != "readonly"
                    ):
                        answer, outcome = await self.verify(
                            p, remaining, request, answer, work
                        )
                    else:
                        outcome = "answered"
                    if mode == "execute-plan":
                        db.execute(
                            "UPDATE session SET mode='develop' WHERE id=? AND mode='plan'",
                            (self.sid,),
                        )
                    if not await self.finish_outcome(outcome):
                        outcome = "blocked"
            status = outcome
            return answer
        except (UnknownToolOutcome, PlanBlocked):
            status = "interrupted"
            raise
        except IncompleteResponse:
            status = "interrupted"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self.s.put(
                self.rid, "elapsed", str(elapsed + max(0, time.time() - started))
            )
            self.s.put(self.rid, "attempt_started", "0")
            self.runtime.log("phase", state=status)
            db.execute("UPDATE run SET status=? WHERE id=?", (status, self.rid))
            db.execute(
                "UPDATE session SET snapshot=? WHERE id=?",
                (self.s.snapshot(), self.sid),
            )
            await self.notify(
                "run",
                f"{status} · {self.rid} · 已记录编辑 {', '.join(self.changes) or '无'}",
            )

    async def resume_delegations(self, p, remaining, external):
        if (
            self.s.get(self.sid, "policy") == "readonly"
            or not p.tools
            or (p.allowed_tools is not None and "delegate" not in p.allowed_tools)
        ):
            return
        rows = self.s.db.execute(
            "SELECT call_id,args FROM checkpoint WHERE rid=? AND name='delegate' AND status='retryable'",
            (self.rid,),
        ).fetchall()
        for row in rows:
            saved = json.loads(row["args"])
            packet = Delegation.model_validate(saved["arguments"])
            if saved["delegation_stage"] != "delegate:" + digest(
                packet.model_dump_json().encode()
            ):
                raise PlanBlocked("委派输入无法从脱敏日志安全恢复，请重新提交任务")
            if remaining[0] <= 0 or remaining[1] <= 0:
                raise PlanBlocked("委派恢复的预算不足")
            remaining[0] -= 1
            self.runtime.hook("PreToolUse", "delegate")
            result = await self.tool(
                row["call_id"],
                "delegate",
                saved["arguments"],
                False,
                external,
                remaining[1],
            )
            self.runtime.hook("PostToolUse", "delegate")
            remaining[1] -= len(result.encode())
            self.s.log(
                self.sid,
                self.rid,
                "result",
                {"name": "delegate", "call_id": row["call_id"], "text": result},
            )
            await self.notify("result", "delegate\n" + bounded(result, 12000))

    def save_plan(self, plan):
        data = plan.model_dump_json()
        self.s.put(self.rid, "plan", data)
        self.s.db.execute("UPDATE session SET plan=? WHERE id=?", (data, self.sid))

    async def execute_plan(self, work):
        plan = Plan.model_validate_json(self.s.get(self.rid, "plan"))
        answers: list[str] = []
        while True:
            index = next(
                (i for i, item in enumerate(plan.steps) if item.status != "done"), None
            )
            if index is None:
                return "\n\n".join(answers) or "计划已执行完毕。"
            step = plan.steps[index]
            if any(
                dep < 1 or dep > index or plan.steps[dep - 1].status != "done"
                for dep in step.dependencies
            ):
                raise PlanBlocked(f"步骤 {index + 1} 的前置步骤尚未完成")
            step.status = "active"
            self.save_plan(plan)
            self.active_step = index
            stage = f"plan:{index + 1}"
            try:
                result = await work(
                    "Implementer",
                    f"只执行计划步骤 {index + 1}：{step.goal}\n验收：{step.acceptance}\n证据要求：{step.evidence_kind or '适用的工具回执'}。后续步骤由执行器调度。",
                    stage,
                )
            finally:
                self.active_step = None
            plan = Plan.model_validate_json(self.s.get(self.rid, "plan"))
            step = plan.steps[index]
            outcomes = {}
            for row in self.s.db.execute(
                "SELECT call_id,name,status,result,args FROM checkpoint WHERE rid=? AND json_extract(args,'$.stage')=? ORDER BY rowid",
                (self.rid, stage),
            ):
                name = row["name"]
                scope = REGISTRY[name][1] if name in REGISTRY else None
                if scope is None:
                    continue
                signature = (
                    name,
                    json.dumps(json.loads(row["args"])["arguments"], sort_keys=True),
                )
                kind = {"read": "read", "edit": "change", "command": "check"}[scope]
                succeeded = row["status"] in {"done", "reconciled"}
                if succeeded and kind == "check":
                    try:
                        check = json.loads(row["result"])
                        succeeded = check["exit_code"] == 0 and not check.get(
                            "cancelled"
                        )
                    except (ValueError, KeyError, TypeError):
                        succeeded = False
                outcomes[signature] = (kind, succeeded, row["call_id"])
            receipts = [
                cid
                for kind, ok, cid in outcomes.values()
                if ok and (not step.evidence_kind or kind == step.evidence_kind)
            ]
            failed = any(not ok and kind != "read" for kind, ok, _ in outcomes.values())
            if not receipts or failed:
                step.status = "blocked"
                self.save_plan(plan)
                self.s.db.execute(
                    "DELETE FROM task_stage WHERE rid=? AND stage=?", (self.rid, stage)
                )
                raise PlanBlocked(
                    f"步骤 {index + 1} 缺少成功的验收证据或存在未解决的失败，后续步骤未启动。"
                )
            step.status = "done"
            step.evidence = ", ".join(receipts)
            self.save_plan(plan)
            answers.append(result)

    async def finish_outcome(self, outcome):
        labels = {
            "answered": "已回答。本轮没有记录到文件修改或通过的项目验证。",
            "changed": "已修改文件，尚未验证。",
            "verified": "已修改文件，已通过本轮列出的验证。",
        }
        failed = self.s.db.execute(
            "SELECT 1 FROM checkpoint WHERE rid=? AND status IN ('failed','unknown','pending') AND name NOT LIKE 'read_%' LIMIT 1",
            (self.rid,),
        ).fetchone()
        if failed and outcome == "answered" and not self.changes:
            await self.notify(
                "outcome", "有工具未成功完成。请查看工具结果，本轮没有验证到改动。"
            )
            return False
        await self.notify("outcome", labels.get(outcome, outcome))
        return True

    async def run_check(self, command, call_id, remaining, attempt):
        if remaining[0] <= 0 or remaining[1] <= 0:
            raise RuntimeError("验证预算不足")
        remaining[0] -= 1
        self.runtime.hook("PreToolUse", "command")
        with FileLock(str(self.s.home / (self.s.wid + ".executor.lock")), timeout=0):
            output = await self.tool(
                call_id, "command", {"command": command}, False, {}, remaining[1]
            )
        self.runtime.hook("PostToolUse", "command")
        remaining[1] -= len(output.encode())
        outcome = json.loads(output)
        self.s.log(
            self.sid,
            self.rid,
            "verification",
            {"command": command, "exit_code": outcome["exit_code"], "attempt": attempt},
        )
        await self.notify("result", "command\n" + bounded(output, 12000))
        return outcome

    async def measure_baseline(self, p, remaining):
        checks = workspace_checks(self.s.start, self.s.root)
        if (
            p.allowed_tools is not None and "command" not in p.allowed_tools
        ) or self.s.get(self.rid, "verification_baseline"):
            return
        baseline = {}
        for command in checks:
            try:
                outcome = await self.run_check(
                    command,
                    "baseline-" + digest(command.encode()),
                    remaining,
                    "baseline",
                )
            except PermissionError:
                continue
            baseline[command] = check_fingerprint(outcome)
        self.s.put(self.rid, "verification_baseline", encoded(baseline))

    async def verify(self, p, remaining, request, answer, work):
        checks = workspace_checks(self.s.start, self.s.root)
        if checks and p.allowed_tools is not None and "command" not in p.allowed_tools:
            raise PermissionError("已修改文件，但模型工具策略不允许执行验证命令")
        if not checks:
            return answer, "changed"
        baseline = json.loads(self.s.get(self.rid, "verification_baseline", "{}"))
        for attempt in range(p.repair_rounds + 1):
            self.runtime.log("phase", state=Phase.VERIFY, attempt=attempt)
            failures, report, existing = [], [], []
            version = self.s.db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM checkpoint WHERE rid=? AND name IN ('edit_file','apply_patch','create_file','command') AND call_id NOT LIKE 'verify-%' AND call_id NOT LIKE 'baseline-%' AND status IN ('done','reconciled')",
                (self.rid,),
            ).fetchone()[0]
            for command in checks:
                call_id = "verify-" + digest(encoded([version, command]).encode())
                outcome = await self.run_check(command, call_id, remaining, attempt)
                report.append(f"{command} → {outcome['exit_code']}")
                if outcome["exit_code"] != 0 or outcome.get("cancelled"):
                    if baseline.get(command) and baseline[command] == check_fingerprint(
                        outcome
                    ):
                        existing.append(command)
                    else:
                        failures.append(bounded(encoded(outcome), 1800))
            if not failures:
                answer += "\n\n验证\n" + "\n".join(report)
                if existing:
                    return answer + "\n与修改前一致的失败：" + "、".join(
                        existing
                    ), "changed"
                return answer, "verified"
            if attempt == p.repair_rounds:
                raise RuntimeError(
                    "验证失败，已达修复轮次上限\n"
                    + "\n".join(report)
                    + "\n"
                    + bounded(failures[0], 1000)
                )
            answer = await work(
                "Implementer",
                request
                + "\nRepair failing checks:\n"
                + bounded("\n".join(failures), 4000),
                f"verification-repair:{attempt}",
            )
        raise RuntimeError("验证未完成")

    @asynccontextmanager
    async def extensions(self, mode, p):
        if (
            mode == "plan"
            or self.s.get(self.sid, "policy") == "readonly"
            or not p.tools
        ):
            yield {}
        else:
            async with connections(self.s, self.notify) as ext:
                yield ext

    async def loop(self, client, p, role, request, external, remaining, chain, stage):
        readonly = (
            role in {"plan", "Explorer", "Reviewer"}
            or self.s.get(self.sid, "policy") == "readonly"
        )
        schemas = [
            schema(n, d, m.model_json_schema())
            for n, (m, scope, d) in REGISTRY.items()
            if not readonly or scope == "read"
        ]
        schemas.append(
            schema(
                "read_result",
                "Read a bounded slice of a completed tool result from this session, using its local reference.",
                OutputRecord.model_json_schema(),
            )
        )
        catalog = available_skills(self.s, self.sid)
        if catalog:
            schemas.append(
                schema(
                    "load_skill",
                    "Choose a relevant workflow. Available names, descriptions and source files: "
                    + encoded(catalog),
                    SkillChoice.model_json_schema(),
                )
            )
            schemas.append(
                schema(
                    "skill_resource",
                    "Read a resource relative to the selected skill.",
                    Resource.model_json_schema(),
                )
            )
        if role in {"plan", "develop", "Implementer"}:
            schemas.append(
                schema(
                    "set_plan",
                    "Plan or revise steps. Set evidence_kind to read, change, or check when the step requires that evidence.",
                    Plan.model_json_schema(),
                )
            )
        if role in {"develop", "plan"}:
            schemas.append(
                schema(
                    "pin_constraint",
                    "Preserve an exact quote of an ongoing requirement from this user request, such as backward compatibility.",
                    Constraint.model_json_schema(),
                )
            )
        if role == "Reviewer":
            schemas.append(
                schema(
                    "report_review",
                    "Return concrete issues, paths and verification evidence for the implementer.",
                    Findings.model_json_schema(),
                )
            )
        if role == "develop" and not readonly:
            schemas.append(
                schema(
                    "delegate",
                    "Delegate a bounded task package with acceptance criteria to an independent role.",
                    Delegation.model_json_schema(),
                )
            )
            schemas.append(
                schema(
                    "save_memory",
                    "Propose an explicit user preference or constraint for confirmation.",
                    Memory.model_json_schema(),
                )
            )
        if not readonly:
            schemas.extend(mcp_schema(external))
        if not p.tools:
            schemas = []
        for profile in (p, self.runtime.parent):
            if profile.allowed_tools is not None:
                schemas = [
                    item
                    for item in schemas
                    if item["function"]["name"] in profile.allowed_tools
                ]
        allowed = {item["function"]["name"] for item in schemas}
        seen: Counter = Counter()
        recent: deque = deque()
        tighter = False
        original = p
        retries = 0
        role_calls = self.s.db.execute(
            """SELECT
            (SELECT count(*) FROM event WHERE rid=? AND kind='phase' AND json_extract(body,'$.state')='request' AND json_extract(body,'$.stage')=?)
            + (SELECT count(*) FROM checkpoint WHERE rid=? AND json_extract(args,'$.stage')=?)
            """,
            (self.rid, stage, self.rid, stage),
        ).fetchone()[0]
        lane = Lane(self.runtime, role)
        while (
            lane.state == Phase.ASSEMBLE
            and remaining[0] > 0
            and role_calls < original.calls
        ):
            ctx = self.builder.build(
                p,
                request,
                schemas,
                chain,
                role,
                tighter,
                role in {"Explorer", "Implementer", "Reviewer"},
            )
            previous_report = json.loads(self.s.get(self.sid, "context", "{}"))
            if ctx.report["summary_until"] > previous_report.get("summary_until", 0):
                self.runtime.log(
                    "compaction",
                    until=ctx.report["summary_until"],
                    used=ctx.report["used"],
                    budget=ctx.report["budget"],
                )
            self.s.put(self.sid, "context", encoded(ctx.report))
            remaining[0] -= 1
            role_calls += 1
            # With a configured spending limit, reserve a whole window if the tokenizer is approximate.
            inputs = ctx.report["used"] + ctx.report["categories"]["tools"]
            reserve_input = (
                max(inputs, p.window - p.output)
                if (self.runtime.parent.cost_limit or p.token_limit)
                and ctx.report["count"] != p.tokenizer
                else inputs
            )
            reservation = self.runtime.reserve(p, role, reserve_input)
            lane.move(
                Phase.REQUEST,
                stage=stage,
                model=p.name,
                context=ctx.report,
                messages=ctx.messages
                if self.s.get(self.sid, "trace_full") == "on"
                else None,
                fingerprint=digest(encoded(ctx.messages).encode()),
                tools=[t["function"]["name"] for t in schemas],
            )
            emitted = False

            async def stream_event(kind, text):
                nonlocal emitted
                emitted = True
                await self.notify(kind, text)

            try:
                await self.runtime.throttle(p)
                if p is original:
                    reply = await complete(
                        client, p, ctx.messages, schemas, stream_event
                    )
                else:
                    async with client_for(p, self.s) as other:
                        reply = await complete(
                            other, p, ctx.messages, schemas, stream_event
                        )
            except (APIConnectionError, APIStatusError) as exc:
                uncertain = emitted or isinstance(exc, APIConnectionError)
                self.runtime.settle(
                    p,
                    role,
                    reservation,
                    reserve_input if uncertain else 0,
                    p.output if uncertain else 0,
                    None,
                    True,
                )
                if (
                    isinstance(exc, BadRequestError)
                    and not tighter
                    and any(
                        w in str(exc).lower()
                        for w in ("context_length", "context window", "maximum context")
                    )
                ):
                    tighter = True
                    lane.move(Phase.ASSEMBLE)
                    continue
                transient = (
                    isinstance(exc, APIConnectionError)
                    or exc.status_code in {408, 409, 429}
                    or exc.status_code >= 500
                )
                if not transient or emitted:
                    raise
                if retries < p.retries:
                    retries += 1
                    delay = min(30.0, 2 ** (retries - 1))
                    if isinstance(exc, APIStatusError):
                        try:
                            delay = max(
                                delay,
                                float(exc.response.headers.get("retry-after", "0")),
                            )
                        except ValueError:
                            pass
                    self.runtime.log("retry", model=p.name, delay=delay)
                    await asyncio.sleep(delay)
                    lane.move(Phase.ASSEMBLE)
                    continue
                fallback = (
                    self.s.find_profile(original.fallback)
                    if original.fallback and p is original
                    else None
                )
                if (
                    not fallback
                    or (schemas and not fallback.tools)
                    or (
                        fallback.allowed_tools is not None
                        and not allowed.issubset(fallback.allowed_tools)
                    )
                ):
                    raise
                p = fallback
                tighter = False
                self.runtime.log("fallback", model=p.name)
                lane.move(Phase.ASSEMBLE)
                continue
            usage = reply.pop("_usage", {})
            self.runtime.settle(
                p,
                role,
                reservation,
                usage.get("prompt_tokens", inputs),
                usage.get("completion_tokens", len(encoded(reply).encode())),
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                not bool(usage),
            )
            retries = 0
            remaining[1] -= len(encoded(reply).encode())
            if remaining[1] < 0:
                raise RuntimeError("本轮达到输出预算")
            if stage != "main":
                for call in reply.get("tool_calls", []):
                    if call["id"]:
                        call["id"] = (
                            "call_" + digest(encoded([stage, call["id"]]).encode())[:40]
                        )
            self.s.log(self.sid, self.rid, "assistant", {**reply, "_stage": stage})
            if not reply.get("tool_calls"):
                lane.move(Phase.COMPLETE)
                return reply["content"]
            group = [reply]
            lane.move(Phase.TOOLS)

            async def dispatch(c):
                nonlocal role_calls
                started = time.monotonic()
                fn = c["function"]
                name = fn["name"]
                if remaining[1] <= 0:
                    raise RuntimeError("本轮达到输出预算")
                if not c["id"]:
                    raise ValueError("端点返回了缺少 ID 的工具调用")
                text = ""
                try:
                    if name not in allowed:
                        raise PermissionError("当前模式未开放该工具")
                    args = json.loads(fn["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须是对象")
                    if remaining[0] <= 0 or role_calls >= original.calls:
                        raise RuntimeError("已达调用预算")
                    remaining[0] -= 1
                    role_calls += 1
                    mutating = name in external or name in {
                        "edit_file",
                        "apply_patch",
                        "create_file",
                        "command",
                    }
                    guard = (
                        FileLock(
                            str(self.s.home / (self.s.wid + ".executor.lock")),
                            timeout=0,
                        )
                        if mutating
                        else nullcontext()
                    )
                    self.runtime.hook("PreToolUse", name)
                    with guard:
                        text = await self.tool(
                            c["id"],
                            name,
                            args,
                            readonly,
                            external,
                            remaining[1],
                            stage=stage,
                        )
                    self.runtime.hook("PostToolUse", name)
                except UnknownToolOutcome:
                    raise
                except Exception as exc:
                    text = "ERROR: " + self.s.clean(str(exc))
                self.runtime.log(
                    "tool_timing",
                    stage=stage,
                    name=name,
                    call_id=c["id"],
                    seconds=time.monotonic() - started,
                    ok=not text.startswith("ERROR:"),
                )
                text = self.s.clean(text)
                self.s.log(
                    self.sid,
                    self.rid,
                    "result",
                    {"call_id": c["id"], "name": name, "text": text},
                )
                await self.notify("result", name + "\n" + bounded(text, 12000))
                remaining[1] -= len(text.encode())
                if remaining[1] < 0:
                    raise RuntimeError("本轮达到输出预算")
                return text

            calls = reply["tool_calls"]
            if len({c["id"] for c in calls}) != len(calls):
                raise ValueError("批次工具调用 ID 重复")
            parallel = (
                len(calls) > 1
                and self.s.get(self.sid, "read", "allow") == "allow"
                and all(
                    c["function"]["name"] in REGISTRY
                    and REGISTRY[c["function"]["name"]][1] == "read"
                    for c in calls
                )
            )
            if parallel:
                async with asyncio.TaskGroup() as tasks:
                    gate = asyncio.Semaphore(4)

                    async def read_one(call):
                        async with gate:
                            return await dispatch(call)

                    pending = [tasks.create_task(read_one(c)) for c in calls]
                results = [t.result() for t in pending]
            else:
                results = [await dispatch(c) for c in calls]
            stalled = False
            for c, text in zip(calls, results, strict=True):
                fn = c["function"]
                name = fn["name"]
                brief = bounded(text, 4000)
                if name == "command":
                    try:
                        cmd = json.loads(text)
                        brief = encoded(
                            {
                                **cmd,
                                "stdout": bounded(cmd["stdout"], 1800),
                                "stderr": bounded(cmd["stderr"], 1800),
                            }
                        )
                    except (ValueError, KeyError):
                        pass
                group.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": brief + f"\n[local run {self.rid}, call {c['id']}]",
                    }
                )
                try:
                    args_key = json.dumps(
                        json.loads(fn["arguments"]),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                except ValueError:
                    args_key = fn["arguments"].strip()
                if name == "command":
                    try:
                        outcome = json.loads(brief)
                        brief_key = encoded(
                            [
                                outcome.get(k)
                                for k in (
                                    "exit_code",
                                    "stdout",
                                    "stderr",
                                    "changed_paths",
                                )
                            ]
                        )
                    except (ValueError, AttributeError):
                        brief_key = brief
                else:
                    brief_key = brief
                signature = (name, args_key, brief_key)
                if name in {
                    "edit_file",
                    "apply_patch",
                    "create_file",
                    "set_plan",
                } and not text.startswith("ERROR:"):
                    seen.clear()
                    recent.clear()
                    self.cycle_replans.discard(stage)
                else:
                    if len(recent) == 8:
                        seen[recent.popleft()] -= 1
                    recent.append(signature)
                    seen[signature] += 1
                    stalled |= seen[signature] >= 3

            if stalled:
                if stage in self.cycle_replans:
                    raise RuntimeError("重规划后仍无进展，已停止重复调用")
                self.cycle_replans.add(stage)
                self.runtime.log("replan", stage=stage, reason="repeated observations")
                group[-1]["content"] += (
                    "\n运行器发现近期调用重复且没有新观察。请改变调查路径或说明阻塞；再次重复将停止。"
                )
            chain.append(group)
            lane.move(Phase.ASSEMBLE)
        raise RuntimeError("本轮达到调用预算")

    async def tool(self, cid, name, args, readonly, external, limit, *, stage="main"):
        db = self.s.db
        old = db.execute(
            "SELECT * FROM checkpoint WHERE rid=? AND call_id=?", (self.rid, cid)
        ).fetchone()
        if old:
            same = (
                old["name"] == name and json.loads(old["args"]).get("arguments") == args
            )
            if same and old["status"] == "done":
                return old["result"]
            if not (same and old["status"] == "retryable"):
                raise RuntimeError("已存在相同调用 ID 的操作，需核对状态")
            db.execute(
                "DELETE FROM checkpoint WHERE rid=? AND call_id=?", (self.rid, cid)
            )
        intent = {"arguments": args, "stage": stage}
        if name == "delegate":
            packet = Delegation.model_validate(args)
            intent["delegation_stage"] = "delegate:" + digest(
                packet.model_dump_json().encode()
            )
        if name in external:
            intent["operation_id"] = digest(
                encoded([self.rid, stage, cid, name]).encode()
            )
        if name in {"edit_file", "create_file", "apply_patch"}:
            model: Edit | Create
            if name == "apply_patch":
                model = self.box.patch_edit(Patch.model_validate(args))
            elif name == "edit_file":
                model = Edit.model_validate(args)
            else:
                model = Create.model_validate(args)
            path = self.box.path(model.path)
            after = (
                model.content
                if isinstance(model, Create)
                else path.read_bytes().decode().replace(model.old, model.new, 1)
            )
            intent["expected"] = digest(after.encode())
            intent["path"] = str(path.relative_to(self.s.root))
        clean_args = json.loads(encoded(intent))

        def scrub(v):
            if isinstance(v, dict):
                return {k: scrub(x) for k, x in v.items()}
            if isinstance(v, list):
                return [scrub(x) for x in v]
            return self.s.clean(v) if isinstance(v, str) else v

        db.execute(
            "INSERT INTO checkpoint VALUES(?,?,?,?,?,?)",
            (self.rid, cid, name, encoded(scrub(clean_args)), "pending", ""),
        )
        await self.notify(
            "action",
            self.s.clean(name + " " + bounded(encoded(args), 3000))
            if name != "command"
            else self.s.clean(args.get("command", "")),
        )
        try:
            if name == "delegate":
                packet = Delegation.model_validate(args)
                if readonly:
                    raise PermissionError("当前角色不可继续委派")
                result = await self.delegate(
                    packet.role, packet.model_dump_json(), intent["delegation_stage"]
                )
            elif name == "read_result":
                record = OutputRecord.model_validate(args)
                row = db.execute(
                    "SELECT result FROM checkpoint c JOIN run r ON r.id=c.rid WHERE r.sid=? AND c.rid=? AND c.call_id=? AND c.status='done'",
                    (self.sid, record.run_id, record.call_id),
                ).fetchone()
                if not row:
                    raise ValueError("当前会话没有这项已完成记录")
                result = row[0][record.start : record.start + record.length]
            elif name == "load_skill":
                choice = SkillChoice.model_validate(args).name
                if choice not in available_skills(self.s, self.sid):
                    raise ValueError("此技能未授权或不存在")
                result = resource(self.s, self.sid, "SKILL.md", choice)
                self.s.put(self.rid, "skill", choice)
            elif name == "skill_resource":
                result = resource(
                    self.s,
                    self.sid,
                    Resource.model_validate(args).path,
                    self.s.get(self.rid, "skill"),
                )
            elif name == "pin_constraint":
                quote = Constraint.model_validate(args).quote
                if quote not in self.request:
                    raise ValueError("约束须逐字引用当前用户需求")
                known = next(
                    (
                        m
                        for m in self.s.memories(self.sid)
                        if m["scope"] == "session" and m["body"] == quote
                    ),
                    None,
                )
                result = (
                    known["id"]
                    if known
                    else self.s.remember(
                        self.sid, "session", quote, "always", "user quote"
                    )
                )
            elif name == "report_review":
                self.review = Findings.model_validate(args)
                result = self.review.model_dump_json()
            elif name == "set_plan":
                plan = Plan.model_validate(args)
                if any(
                    any(dep < 1 or dep >= i for dep in st.dependencies)
                    for i, st in enumerate(plan.steps, 1)
                ):
                    raise ValueError("依赖必须指向较早步骤（从 1 开始）")
                if any(
                    step.status == "done" and not step.evidence.strip()
                    for step in plan.steps
                ):
                    raise ValueError("完成步骤需要实际验收依据")
                revisions = db.execute(
                    "SELECT count(*) FROM checkpoint WHERE rid=? AND name='set_plan' AND status='done'",
                    (self.rid,),
                ).fetchone()[0]
                if revisions >= 4:
                    raise ValueError("本轮计划修订已达上限，请报告剩余问题")
                if self.active_step is not None and stage.startswith("plan:"):
                    old = Plan.model_validate_json(self.s.get(self.rid, "plan"))
                    prefix = old.steps[: self.active_step + 1]
                    if len(plan.steps) < len(prefix) or any(
                        (new.goal, new.dependencies, new.acceptance, new.evidence_kind)
                        != (
                            previous.goal,
                            previous.dependencies,
                            previous.acceptance,
                            previous.evidence_kind,
                        )
                        for new, previous in zip(plan.steps, prefix)
                    ):
                        raise ValueError("重规划只允许更新尚未开始的步骤")
                    plan.steps = prefix + [
                        step.model_copy(update={"status": "pending", "evidence": ""})
                        for step in plan.steps[len(prefix) :]
                    ]
                    self.save_plan(Plan.model_validate(scrub(plan.model_dump())))
                elif readonly and any(step.status == "done" for step in plan.steps):
                    raise ValueError("规划阶段不能将步骤标为已完成")
                data = encoded(scrub(plan.model_dump()))
                db.execute("UPDATE session SET plan=? WHERE id=?", (data, self.sid))
                result = data
            elif name == "save_memory":
                m = Memory.model_validate(args)
                if await self.approve("memory", m.scope + " " + m.content) not in {
                    "once",
                    "session",
                }:
                    raise PermissionError("未确认记忆")
                result = self.s.remember(
                    self.sid, m.scope, m.content, m.tag, "confirmed"
                )
            elif name in external:
                await self.box.permission("external", name + " " + encoded(args))
                result = await invoke(external, name, args, intent["operation_id"])
            else:
                if name == "command":
                    self.box.command_changes = []
                try:
                    result = await self.box.call(
                        name, args, readonly, limit, journal=(self.rid, cid)
                    )
                finally:
                    if name == "command" and self.box.command_changes:
                        self.changes.extend(self.box.command_changes)
                        self.s.log(
                            self.sid,
                            self.rid,
                            "changed_paths",
                            self.box.command_changes,
                        )
                if name in {"edit_file", "apply_patch", "create_file", "read_file"}:
                    relative = str(self.box.path(args["path"]).relative_to(self.s.root))
                    version = (
                        result.split("\n", 1)[0].removeprefix("version=")
                        if name == "read_file"
                        else intent["expected"]
                    )
                    db.execute(
                        "UPDATE observation SET valid=0 WHERE sid=? AND path=? AND version<>?",
                        (self.sid, relative, version),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO observation(sid,rid,call_id,path,version) VALUES(?,?,?,?,?)",
                        (self.sid, self.rid, cid, relative, version),
                    )
                if name in {"edit_file", "apply_patch", "create_file"}:
                    self.changes.append(args["path"])
        except Exception as exc:
            status = "unknown" if isinstance(exc, UnknownToolOutcome) else "failed"
            db.execute(
                "UPDATE checkpoint SET status=?,result=? WHERE rid=? AND call_id=?",
                (status, self.s.clean(str(exc)), self.rid, cid),
            )
            if status == "unknown":
                self.runtime.log(
                    "external_outcome",
                    tool=name,
                    call_id=cid,
                    operation_id=intent["operation_id"],
                    status=status,
                )
            raise
        db.execute(
            "UPDATE checkpoint SET status='done',result=? WHERE rid=? AND call_id=?",
            (self.s.clean(result), self.rid, cid),
        )
        return result
