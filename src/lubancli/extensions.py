"""Lazy skill discovery and one lifecycle for each trusted stdio MCP connection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ConfigDict, Field

from .state import Store
from .tools import schema


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1)
    field: str = Field(min_length=1)


class Server(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(min_length=1)
    args: list[str] = []
    idempotency: dict[str, str] = Field(default_factory=dict)
    receipts: dict[str, Receipt] = Field(default_factory=dict)


def skills(s: Store) -> dict[str, dict]:
    found = {}
    for parent in (
        Path(__file__).parent / "skills",
        s.home / "skills",
        s.root / ".luban" / "skills",
    ):
        if (
            parent == s.root / ".luban" / "skills"
            and not parent.resolve().is_relative_to(s.root)
        ):
            continue
        for file in parent.glob("*/SKILL.md"):
            try:
                if (
                    not file.resolve().is_relative_to(parent.resolve())
                    or file.stat().st_size > 128000
                ):
                    continue
                with file.open() as f:
                    header = f.read(2048)
                match = re.match(
                    r"---\s*\nname:\s*([\w-]+)\s*\ndescription:\s*([^\n]+)\n---", header
                )
                if match:
                    found[match[1]] = {"description": match[2], "file": str(file)}
            except (OSError, UnicodeError):
                continue
    return found


def servers(s: Store) -> dict[str, Server]:
    p = s.root / ".luban" / "mcp.json"
    if not p.exists():
        return {}
    if not p.resolve().is_relative_to(s.root) or p.stat().st_size > 100000:
        raise ValueError("非法 MCP 配置路径或大小")
    return {
        name: Server.model_validate(raw)
        for name, raw in json.loads(p.read_text()).items()
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", name)
    }


def fingerprint(server: Server) -> str:
    extras = {
        field for field in ("idempotency", "receipts") if not getattr(server, field)
    }
    return hashlib.sha256(server.model_dump_json(exclude=extras).encode()).hexdigest()


@dataclass(frozen=True)
class ExternalTool:
    client: Any
    spec: Any
    idempotency: str | None = None
    receipt: Receipt | None = None


@asynccontextmanager
async def connections(s: Store, notify):
    tools: dict[str, ExternalTool] = {}
    async with AsyncExitStack() as stack:
        try:
            config = servers(s)
        except Exception as exc:
            config = {}
            await notify("mcp", str(exc))
        for name, server in config.items():
            if s.get(s.wid, "mcp:" + name) != fingerprint(server):
                continue
            conn = AsyncExitStack()
            try:
                # Enter and exit SDK task groups from the same task.
                err = conn.enter_context(open(os.devnull, "w"))
                streams = await conn.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=server.command, args=server.args, cwd=s.start
                        ),
                        errlog=err,
                    )
                )
                client = await conn.enter_async_context(ClientSession(*streams))
                async with asyncio.timeout(12):
                    await client.initialize()
                    listing = await client.list_tools()
                listed = {item.name: item for item in listing.tools}
                for item in listing.tools:
                    key = f"mcp__{name}__{item.name}"
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
                        continue
                    field = server.idempotency.get(item.name)
                    receipt = server.receipts.get(item.name)
                    lookup = listed.get(receipt.tool) if receipt else None
                    if field and field not in item.inputSchema.get("properties", {}):
                        await notify("mcp", f"{key} 的幂等字段未出现在工具 schema 中")
                        continue
                    if receipt and (
                        not field
                        or not lookup
                        or receipt.tool == item.name
                        or receipt.field not in lookup.inputSchema.get("properties", {})
                    ):
                        await notify("mcp", f"{key} 的回执查询配置与工具 schema 不匹配")
                        continue
                    tools[key] = ExternalTool(client, item, field, receipt)
                stack.push_async_callback(conn.pop_all().aclose)
            except Exception as exc:
                await notify("mcp", f"{name}: {exc}")
            finally:
                await conn.aclose()
        yield tools


def mcp_schema(items) -> list[dict]:
    result = []
    for key, item in items.items():
        tool, field = item.spec, item.idempotency
        spec = tool.inputSchema
        if field:
            spec = {
                **spec,
                "properties": {
                    k: v for k, v in spec["properties"].items() if k != field
                },
                "required": [k for k in spec.get("required", []) if k != field],
            }
        result.append(schema(key, tool.description or key, spec))
    return result


class UnknownToolOutcome(RuntimeError):
    """The external service may have applied a change before losing its reply."""


async def invoke(items, key, args, operation_id: str = "") -> str:
    item = items[key]
    client, tool, field = item.client, item.spec, item.idempotency
    if field and not operation_id:
        raise ValueError("已配置幂等字段，但此调用没有持久化操作 ID")
    if field:
        args = {**args, field: operation_id}
    jsonschema.validate(args, tool.inputSchema)
    for attempt in range(2 if field else 1):
        try:
            async with asyncio.timeout(30):
                result = await client.call_tool(tool.name, args)
        except Exception as exc:
            if item.receipt:
                receipt = await lookup_receipt(item, operation_id)
                if receipt is not None:
                    return receipt
            if isinstance(exc, TimeoutError) and field and attempt == 0:
                continue
            raise UnknownToolOutcome(
                f"{key} 的外部结果未确认。请核对服务状态后使用 /ack。"
            ) from exc
        if result.isError:
            raise ValueError(
                "MCP 工具报告失败：" + result.model_dump_json(exclude_none=True)
            )
        return result.model_dump_json(exclude_none=True)
    raise RuntimeError("外部工具未返回结果")


async def lookup_receipt(item: ExternalTool, operation_id: str) -> str | None:
    receipt = item.receipt
    if receipt is None or not operation_id:
        return None
    try:
        async with asyncio.timeout(8):
            response = await item.client.call_tool(
                receipt.tool, {receipt.field: operation_id}
            )
    except Exception:
        return None
    data = response.structuredContent
    if (
        response.isError
        or not isinstance(data, dict)
        or data.get("operation_id") != operation_id
    ):
        return None
    if data.get("status") == "failed":
        raise ValueError("外部服务回执确认操作失败")
    if data.get("status") == "succeeded":
        return response.model_dump_json(exclude_none=True)
    return None


def resource(s: Store, sid: str, relative: str, selected: str = "") -> str:
    """Read a supporting document beneath the explicitly selected skill."""
    name = selected or s.get(sid, "skill")
    item = skills(s).get(name)
    if not item:
        raise ValueError("请先通过 /skills use NAME 选择技能")
    root = Path(item["file"]).resolve().parent
    target = (root / relative).resolve()
    if (
        not target.is_relative_to(root)
        or not target.is_file()
        or target.stat().st_size > 64000
    ):
        raise ValueError("技能资源越界、不存在或超过 64 KB")
    return target.read_text()


def available_skills(s: Store, sid: str) -> dict[str, dict]:
    project = s.root / ".luban" / "skills"
    return {
        k: v
        for k, v in skills(s).items()
        if not Path(v["file"]).is_relative_to(project)
        or s.get(s.wid, "trust") == "yes"
        or s.get(sid, "skill") == k
    }
