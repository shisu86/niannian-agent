"""长期记忆抽取与写入（S4）。

链路：reflect → extract_memories（LLM JSON mode）→ 校验 → save_memories
（一个事务里写 memory_item + memory_fts，并标记被取代的旧记忆）。

原则：
- 模型只负责"抽什么"，写入前的合法性由服务端兜底；
- 写入失败整批回滚，不留下"列表有、检索没有"的半截数据；
- 危机分支在 reflect 层就被挡住，这里不处理安全逻辑。
"""

import asyncio
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import connect, utc_now
from app.services.llm import complete_json
from app.services.persona import ensure_persona
from app.services.search import tokenize

logger = logging.getLogger(__name__)

# 同一进程内串行化"抽取 + 写入"。
# 两轮同时 reflect 时，后一轮会等前一轮写完再读记忆，冲突判定更准确，
# 也不会出现两轮都认为同一条旧记忆可取代、最后写入两条矛盾记忆。
write_lock = asyncio.Lock()

# 本轮允许写入的类型；summary 由会话摘要任务产生，不在对话抽取里生成
WRITABLE_TYPES = {"fact", "preference", "event", "relationship", "followup"}

MAX_CONTENT_LEN = 200  # 单条记忆上限，防止模型写小作文挤占上下文
MAX_RECENT_MEMORIES = 20  # 传给模型的已有记忆数量，用于冲突判定
MIN_USER_TEXT_LEN = 4  # 少于这个长度的话（嗯、好的）不可能有信息量

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

EXTRACT_PROMPT = """你是对话记忆抽取器。从一轮对话中抽取值得长期记住的内容。

可用的 type：
- fact：身份、居住地、工作、健康、家庭成员等客观事实
- preference：喜好、厌恶、习惯
- event：最近发生的事
- relationship：和身边人的关系、矛盾、互动
- followup：未来要跟进的具体事项，必须给出 due_at

importance 评分：
5 = 长期重要（工作、健康、重大计划、亲密关系）
3 = 一般事实
1 = 琐碎、短期、寒暄

规则：
1. 只记用户明确说过的，不要推测、不要编造。
2. 寒暄、感叹、没有信息量的情绪发泄不要记，返回空数组。
3. 新信息推翻已有记忆时（住址变了、工作变了），在 replaces 里填旧记忆的 id；
   没有冲突就填空数组。
4. followup 必须把"下周三"这类相对时间换算成绝对时间，输出 UTC，
   格式 2026-12-02T02:00:00Z。
5. 每条 content 是一句完整的中文陈述，不超过 80 字。

只输出 JSON，不要任何解释：
{"items":[{"type":"fact","content":"用户在杭州做产品运营","importance":5,
"due_at":null,"replaces":[]}]}
"""


def _local_now_text() -> str:
    """当前本地时间文本，让模型能把"下周三"换算成绝对时间。"""
    now = datetime.now(ZoneInfo(get_settings().tz))
    return f"{now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}）"


def _normalize_due_at(value) -> str | None:
    """把模型给的时间统一成 UTC 定长格式；解析失败返回 None。

    带时区的按原时区换算；不带时区的按用户本地时区理解——
    prompt 给模型的是本地时间，模型漏写时区时按本地解释更不容易错。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(get_settings().tz))
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_items(raw_items, valid_ids: set[int]) -> list[dict]:
    """逐条校验模型输出，不合格的单条丢弃，不连累其他条目。

    valid_ids 是当前 persona 的 active 记忆 id，replaces 只能指向它们，
    避免模型编造 id 或去取代别人的记忆。
    """
    if not isinstance(raw_items, list):
        logger.warning("memory extract: items is not a list")
        return []

    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        # 类型先确认是字符串再查白名单：list/dict 不可哈希，直接 in 会抛异常
        item_type = raw.get("type")
        if not isinstance(item_type, str) or item_type not in WRITABLE_TYPES:
            continue

        # content 只接受字符串；数组/对象转 str 会把 Python repr 写进库
        raw_content = raw.get("content")
        if not isinstance(raw_content, str):
            continue
        content = raw_content.strip()
        if not content:
            continue
        content = content[:MAX_CONTENT_LEN]

        # bool 是 int 的子类，True 会被悄悄当成 1，这里显式排除
        raw_importance = raw.get("importance", 3)
        if isinstance(raw_importance, bool) or raw_importance is None:
            continue
        try:
            importance = int(raw_importance)
        except (TypeError, ValueError):
            continue
        if not 1 <= importance <= 5:
            continue

        # followup 没有可执行时间就没有意义；其他类型一律清空 due_at
        due_at = _normalize_due_at(raw.get("due_at")) if item_type == "followup" else None
        if item_type == "followup" and not due_at:
            continue

        replaces: list[int] = []
        raw_replaces = raw.get("replaces") or []
        if isinstance(raw_replaces, list):
            for value in raw_replaces:
                # 同样排除 bool，避免 true 被当成 id=1
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    old_id = value
                elif isinstance(value, str) and value.isdigit():
                    old_id = int(value)
                else:
                    continue
                if old_id in valid_ids and old_id not in replaces:
                    replaces.append(old_id)

        # 同批内按 (类型, 内容) 去重，防止模型一次返回两条相同记忆
        key = (item_type, content)
        if key in seen:
            continue
        seen.add(key)

        cleaned.append(
            {
                "type": item_type,
                "content": content,
                "importance": importance,
                "due_at": due_at,
                "replaces": replaces,
            }
        )
    return cleaned


async def _recent_memories(persona_id: int) -> list[dict]:
    """取最近的 active 记忆，给模型做冲突判定与去重参考。"""
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT id, content
            FROM memory_item
            WHERE persona_id = ? AND status = 'active'
            ORDER BY created_at DESC, importance DESC
            LIMIT ?
            """,
            (persona_id, MAX_RECENT_MEMORIES),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"id": row["id"], "content": row["content"]} for row in rows]
    finally:
        await conn.close()


async def extract_memories(persona_id: int, user_text: str, ai_text: str) -> list[dict]:
    """调模型抽取本轮记忆，返回校验后的条目列表。

    ai_text 只作为理解语境用，抽取目标始终是"用户说过的话"。
    """
    recent = await _recent_memories(persona_id)
    recent_lines = "\n".join(f"{item['id']}: {item['content']}" for item in recent) or "（无）"

    messages = [
        {"role": "system", "content": EXTRACT_PROMPT},
        {
            "role": "user",
            "content": (
                f"当前本地时间：{_local_now_text()}\n\n"
                f"用户说：{user_text}\n\n"
                f"助理回复：{ai_text}\n\n"
                f"已有记忆（id: 内容）：\n{recent_lines}\n\n"
                "请输出 JSON。"
            ),
        },
    ]

    data = await complete_json(messages)
    raw_items = data.get("items")
    # 顶层键写错不能当成"成功抽到 0 条"，否则记忆会永远静默不写
    if not isinstance(raw_items, list):
        raise ValueError("模型返回缺少 items 数组")

    items = _validate_items(raw_items, {item["id"] for item in recent})
    logger.info("memory extract: raw=%s valid=%s", len(raw_items), len(items))
    return items


async def save_memories(persona_id: int, items: list[dict]) -> dict:
    """一个事务里写完新记忆、FTS 索引与 superseded 标记。

    返回 {"written": 新条数, "superseded": 被取代条数}。
    任何一步失败都会整批回滚，不会出现两张表不一致。
    """
    if not items:
        return {"written": 0, "superseded": 0}

    # 外键前提：全新部署时 init_db 不预插 persona，这里兜底创建
    await ensure_persona()

    conn = await connect()
    try:
        now = utc_now()
        written = 0
        superseded = 0

        for item in items:
            cursor = await conn.execute(
                """
                INSERT INTO memory_item
                  (persona_id, type, content, content_tok, importance, status,
                   due_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    persona_id,
                    item["type"],
                    item["content"],
                    tokenize(item["content"]),
                    item["importance"],
                    item["due_at"],
                    now,
                    now,
                ),
            )
            new_id = cursor.lastrowid

            # rowid 与 memory_item.id 对齐，检索时用 JOIN 取回原文
            await conn.execute(
                "INSERT INTO memory_fts(rowid, content_tok) VALUES (?, ?)",
                (new_id, tokenize(item["content"])),
            )

            for old_id in item["replaces"]:
                update = await conn.execute(
                    """
                    UPDATE memory_item
                    SET status = 'superseded', superseded_by = ?, updated_at = ?
                    WHERE id = ? AND persona_id = ? AND status = 'active'
                    """,
                    (new_id, now, old_id, persona_id),
                )
                superseded += update.rowcount

            written += 1

        await conn.commit()
        logger.info("memory saved: written=%s superseded=%s", written, superseded)
        return {"written": written, "superseded": superseded}
    except Exception:
        # 事务性写入失败：整批回滚，避免半截数据
        await conn.rollback()
        logger.exception("memory save failed, rolled back")
        raise
    finally:
        await conn.close()
