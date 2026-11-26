"""长期记忆检索。

链路：jieba 分词 → FTS5 MATCH → 合并去重 → 重排 Top-K。
规则：
- 只检索 status='active'；
- token 加双引号，避免 FTS5 特殊字符注入；
- token 间默认 AND，无结果降级 OR；
- 重排 = 0.65 × 相关度 + 0.20 × 重要性 + 0.15 × 新近度。
"""

import asyncio
import logging
import math
import re
from datetime import UTC, datetime

import aiosqlite
import jieba

from app.db import connect, utc_now

logger = logging.getLogger(__name__)

DEFAULT_K = 5
CANDIDATE_LIMIT = 10

# FTS5 遇到 NUL 等控制字符会抛 "unterminated string"，写入与查询前都剔除
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def tokenize(text: str) -> str:
    """与写入时保持同一套分词逻辑，否则索引和查询对不上。"""
    cleaned = _CONTROL_CHARS.sub("", text)
    return " ".join(token.strip() for token in jieba.lcut(cleaned) if token.strip())


def _fts_expression(text: str, operator: str) -> str:
    """把查询切成 token，逐个加双引号后按 AND/OR 连接。"""
    cleaned = _CONTROL_CHARS.sub("", text)
    tokens = [token.strip().replace('"', '""') for token in jieba.lcut(cleaned) if token.strip()]
    if not tokens:
        return ""
    return f" {operator} ".join(f'"{token}"' for token in tokens)


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _recency(created_at: str) -> float:
    """新近度：30 天衰减，格式非法时给中间值，避免影响排序稳定性。"""
    try:
        created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0.5
    days = max((datetime.now(UTC) - created).total_seconds() / 86400, 0)
    return math.exp(-days / 30)


def _rerank(items: list[dict]) -> list[dict]:
    """按文档公式重排。

    SQLite 的 bm25 是负数：越相关越负。取绝对值后映射为
    relevance = |rank| / (1 + |rank|)，保证强匹配得分更高；
    不能用 1/(1+|rank|)，那会把方向弄反。
    """
    for item in items:
        rank = abs(float(item.get("rank") or 0))
        relevance = rank / (1 + rank)
        importance = float(item.get("importance") or 3) / 5
        recency = _recency(item.get("created_at") or "")
        item["score"] = round(0.65 * relevance + 0.20 * importance + 0.15 * recency, 4)
    return sorted(items, key=lambda item: item["score"], reverse=True)


async def _query_fts(
    conn: aiosqlite.Connection,
    persona_id: int,
    expression: str,
    limit: int,
) -> list[dict]:
    sql = """
        SELECT m.id, m.type, m.content, m.importance, m.status, m.created_at,
               bm25(memory_fts) AS rank
        FROM memory_fts
        JOIN memory_item m ON m.id = memory_fts.rowid
        WHERE memory_fts MATCH ?
          AND m.persona_id = ?
          AND m.status = 'active'
        ORDER BY rank
        LIMIT ?
    """
    async with conn.execute(sql, (expression, persona_id, limit)) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def warmup() -> None:
    """预热 jieba 词典，避免首个请求在事件循环里阻塞几百毫秒。"""
    await asyncio.to_thread(jieba.initialize)


async def search(
    persona_id: int,
    queries: list[str],
    k: int = DEFAULT_K,
    mode: str = "keyword",
    update_hits: bool = True,
) -> list[dict]:
    """多 query 检索并重排。

    update_hits=False 供评估脚本使用，避免污染“被想起次数”。
    """
    if mode != "keyword":
        # 预留 hybrid：接入 Embedding 后实现，当前显式拒绝而不是静默降级
        raise NotImplementedError("hybrid 检索尚未实现")

    candidates: dict[int, dict] = {}
    conn = await connect()
    try:
        for query in queries:
            if not query.strip():
                continue
            and_expr = _fts_expression(query, "AND")
            if not and_expr:
                continue
            rows = await _query_fts(conn, persona_id, and_expr, CANDIDATE_LIMIT)
            if not rows:
                # 长句 AND 太严时降级为 OR，宁可多召回再重排
                or_expr = _fts_expression(query, "OR")
                if or_expr:
                    rows = await _query_fts(conn, persona_id, or_expr, CANDIDATE_LIMIT)
            for row in rows:
                existing = candidates.get(row["id"])
                # 同一记忆被多个 query 命中时，保留相关度最高（|bm25| 最大）的一条
                if existing is None or abs(float(row["rank"])) > abs(float(existing["rank"])):
                    candidates[row["id"]] = row

        ranked = _rerank(list(candidates.values()))[:k]

        if update_hits and ranked:
            now = utc_now()
            # executemany 避免拼接 IN 列表，既无注入面也更清晰
            await conn.executemany(
                "UPDATE memory_item SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?",
                [(now, item["id"]) for item in ranked],
            )
            await conn.commit()

        return ranked
    finally:
        await conn.close()
