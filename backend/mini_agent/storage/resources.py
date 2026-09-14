"""``ResourceStore``：把大文本存成"磁盘文件 + 数据库索引"，支持分页读取与检索。

典型来源是"工具结果太大"时转存，之后模型用 resource_read / resource_search 分页取用，
避免一次性把大段内容塞进上下文。
"""
import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select

from ..contracts import ExecutionContext, ToolResult
from .schema import resources, utc_now
from .store import Store

# 清理失败之类的"可继续但需要留痕"的情况走日志，不打断调用方。
logger = logging.getLogger(__name__)


class ResourceStore:
    """大文本的存取：磁盘文件 + 数据库索引。

    典型来源是"工具结果太大"时转存，之后模型用 resource_read / resource_search 分页取用，
    避免一次性把大段内容塞进上下文。
    """
    MAX_BYTES = 10 * 1024 * 1024

    def __init__(self, store: Store, root: Path) -> None:
        self.store, self.root = store, root
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, session_id: str, content: str, kind: str = "text") -> str:
        data = content.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ValueError("resource_too_large")
        resource_id, file_key = str(uuid4()), f"{uuid4()}.txt"
        # 先写临时文件再原子替换：中途失败不会留下半截文件被后续读取。
        final_path, temp_path = self.root / file_key, self.root / f".{file_key}.tmp"
        await asyncio.to_thread(temp_path.write_bytes, data)
        await asyncio.to_thread(temp_path.replace, final_path)
        try:
            async with self.store.engine.begin() as conn:
                await conn.execute(insert(resources).values(
                    id=resource_id,
                    session_id=session_id,
                    kind=kind,
                    file_key=file_key,
                    size=len(data),
                    created_at=utc_now()
                ))
        except Exception:
            # 索引写失败就把刚落的文件删掉，避免出现"有文件没记录"的孤儿。
            # 清理失败不能顶掉真正的失败原因：原异常必须照常抛出，否则报出来的是
            # 文件删除错误，而真正的问题（索引写失败）反而被掩盖。
            try:
                await asyncio.to_thread(final_path.unlink, missing_ok=True)
            except BaseException:
                logger.warning("failed to remove orphaned resource file %s", file_key, exc_info=True)
            raise
        return resource_id

    async def purge_files(self, file_keys: list[str]) -> int:
        """删除给定的资源文件，返回处理过的文件数。

        只碰磁盘、不碰数据库：调用方必须已经提交了资源索引的删除。顺序反过来
        （先删文件再删索引）会在索引删除失败时留下"有记录、无文件"的不可读资源。

        清理是"尽力而为"：索引已经删掉了，文件残留只是整洁问题，所以这里吞掉所有异常
        （除取消信号外）——包括权限不足、句柄占用，以及平台删除防护抛出的 ``SystemExit``
        这类非 ``Exception`` 异常。否则清理失败会把一次已经提交的删除报成 500，让客户端
        误以为删除失败而重试，而会话其实早已不存在。
        """
        handled = 0
        for file_key in file_keys:
            try:
                (self.root / file_key).unlink(missing_ok=True)
                handled += 1
            except asyncio.CancelledError:
                # 取消必须继续向上传播：吞掉它会让请求取消被清理循环拖住。
                raise
            except BaseException:
                logger.warning("failed to purge resource file %s", file_key, exc_info=True)
        return handled

    async def _load(self, resource_id: str, session_id: str) -> str | None:
        # 查询条件带上 session_id：资源不可跨会话读取。
        async with self.store.engine.connect() as conn:
            row = (await conn.execute(select(resources.c.file_key).where(
                resources.c.id == resource_id,
                resources.c.session_id == session_id
            ))).first()
        if not row:
            return None
        try:
            return await asyncio.to_thread((self.root / row[0]).read_text, encoding="utf-8")
        except FileNotFoundError:
            # 索引还在但文件被外部删掉了，按"找不到"处理而不是抛错。
            return None

    async def read_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        """按游标分页读取资源内容。"""
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={
                "code": "resource_not_found",
                "message": "找不到指定资源，或资源不属于当前会话。",
                "outcome": "failed"
            })
        # token 预算换算成字符数（≈1 token 3 字符），再扣掉一点信封开销，保证返回结果不超预算。
        budget_chars = max(64, (ctx.result_token_budget or 4000) * 3 - 384)
        cursor, requested = args.get("cursor", 0), min(args.get("limit", 4000), budget_chars, 20_000)
        chunk = content[cursor:cursor + requested]
        next_cursor = cursor + len(chunk)
        # next_cursor 为 None 明确的告诉模型"已经读完了"，避免它无谓地继续翻页。
        return ToolResult(True, {
            "content": chunk,
            "cursor": cursor,
            "next_cursor": next_cursor if next_cursor < len(content) else None,
            "end": next_cursor >= len(content)
        })

    async def search_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        """在资源里定位关键词，返回带上下文的片段与下一次搜索的起点。"""
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={
                "code": "resource_not_found",
                "message": "找不到指定资源，或资源不属于当前会话。",
                "outcome": "failed"
            })
        start = content.find(args["query"], args.get("cursor", 0))
        if start < 0:
            return ToolResult(True, {"matches": [], "next_cursor": None})
        budget_chars = max(64, (ctx.result_token_budget or 800) * 3 - 384)
        # 片段长度也受预算约束：片段 = 关键词 + 左右各一半上下文。
        context_chars = max(32, min(400, budget_chars - len(args["query"])))
        left, right = max(0, start - context_chars // 2), min(
            len(content),
            start + len(args["query"]) + context_chars // 2
        )
        # next_cursor 指向本次匹配之后，便于模型继续找下一处。
        return ToolResult(True, {"matches": [{
            "position": start,
            "snippet": content[left:right]
        }], "next_cursor": start + len(args["query"])})
