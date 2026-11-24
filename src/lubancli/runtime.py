"""Per-run accounting and workspace-owned lifecycle policies."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .state import Profile, Store, uid


class Phase(StrEnum):
    ASSEMBLE = "assemble"
    REQUEST = "request"
    TOOLS = "tools"
    VERIFY = "verify"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Lane:
    """Enforce legal transitions within one role invocation."""

    allowed = {
        Phase.ASSEMBLE: {Phase.REQUEST},
        Phase.REQUEST: {Phase.ASSEMBLE, Phase.TOOLS, Phase.COMPLETE},
        Phase.TOOLS: {Phase.ASSEMBLE},
        Phase.COMPLETE: set(),
    }

    def __init__(self, runtime, role):
        self.runtime, self.role, self.id = runtime, role, uid()
        self.state = Phase.ASSEMBLE
        runtime.log("phase", lane=self.id, state=self.state, role=role)

    def move(self, state, **detail):
        if state not in self.allowed[self.state]:
            raise RuntimeError(f"非法状态迁移 {self.state} → {state}")
        self.runtime.log(
            "phase",
            lane=self.id,
            previous=self.state,
            state=state,
            role=self.role,
            **detail,
        )
        self.state = state


class Hook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(pattern="^(PreToolUse|PostToolUse)$")
    tool: str = "*"
    action: str = Field(pattern="^(trace|deny)$")
    message: str = ""


class Runtime:
    def __init__(self, store: Store, sid: str, rid: str, parent: Profile):
        self.store, self.sid, self.rid, self.parent = store, sid, rid, parent
        self.cost = 0.0
        self.used: dict[str, int] = {}
        self.hooks = [
            Hook.model_validate(item)
            for item in json.loads(store.get(sid, "hooks", "[]"))
        ]
        for (raw,) in store.db.execute(
            "SELECT body FROM event WHERE rid=? AND kind='usage'", (rid,)
        ):
            row = json.loads(raw)
            self.cost += row["cost"]
            self.used[row["role"]] = self.used.get(row["role"], 0) + row["tokens"]

        for role, tokens, cost in store.db.execute(
            "SELECT role,tokens,cost FROM reservation WHERE rid=?", (rid,)
        ):
            self.cost += cost
            self.used[role] = self.used.get(role, 0) + tokens

    def log(self, kind, **data):
        self.store.log(self.sid, self.rid, kind, {"time": time.time(), **data})

    def hook(self, event, name):
        for hook in self.hooks:
            if hook.event == event and fnmatch.fnmatchcase(name, hook.tool):
                self.log(
                    "hook",
                    event=event,
                    tool=name,
                    action=hook.action,
                    message=hook.message,
                )
                if hook.action == "deny":
                    raise PermissionError(hook.message or "工具策略拒绝此调用")

    def reserve(self, p, role, inputs):
        tokens = inputs + p.output
        cost = (inputs * p.input_price + p.output * p.output_price) / 1_000_000
        if self.parent.cost_limit and (not p.input_price or not p.output_price):
            raise ValueError("启用费用预算需要为每个模型配置输入和输出单价")
        if self.parent.cost_limit and self.cost + cost > self.parent.cost_limit:
            raise RuntimeError("本轮费用预算不足")
        if (
            self.parent.token_limit
            and sum(self.used.values()) + tokens > self.parent.token_limit
        ):
            raise RuntimeError("本轮 Token 预算不足")
        if p.token_limit and self.used.get(role, 0) + tokens > p.token_limit:
            raise RuntimeError("角色 Token 预算不足")
        self.cost += cost
        self.used[role] = self.used.get(role, 0) + tokens
        key = uid()
        self.store.db.execute(
            "INSERT INTO reservation VALUES(?,?,?,?,?)",
            (key, self.rid, role, tokens, cost),
        )
        return tokens, cost, key

    def settle(self, p, role, reservation, inputs, outputs, cached, estimated):
        tokens = inputs + outputs
        cost = (inputs * p.input_price + outputs * p.output_price) / 1_000_000
        self.cost += cost - reservation[1]
        self.used[role] += tokens - reservation[0]
        db = self.store.db
        db.execute("SAVEPOINT settlement")
        try:
            self.log(
                "usage",
                role=role,
                model=p.name,
                input=inputs,
                output=outputs,
                cached=cached,
                tokens=tokens,
                cost=cost,
                estimated=estimated,
                priced=bool(p.input_price and p.output_price),
            )
            db.execute("DELETE FROM reservation WHERE id=?", (reservation[2],))
            db.execute("RELEASE settlement")
        except BaseException:
            db.execute("ROLLBACK TO settlement")
            db.execute("RELEASE settlement")
            raise

    async def throttle(self, p):
        if p.rpm:
            db = self.store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                start = max(
                    now, float(self.store.get("*", "next_request:" + p.name, "0"))
                )
                self.store.put("*", "next_request:" + p.name, str(start + 60 / p.rpm))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            await asyncio.sleep(start - now)
