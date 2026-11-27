"""SQLite 连接与幂等建表。

两个库分开：
- app.db：业务数据与长期记忆（本模块负责）
- checkpoints.db：LangGraph 会话状态，由 AsyncSqliteSaver 自动管理

分文件是为了避免 checkpointer 的高频写入与业务表争抢 SQLite 写锁。
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

# 全部使用 CREATE ... IF NOT EXISTS，保证服务重启时幂等执行。
# 表结构以开发文档第 6 节为准，改表时同步更新这里。
# 字段约束尽量写进 DDL：脏数据在入库阶段就被挡住，比只在 Python 层校验更可靠。
# 注意：DDL 变更只对新建的库生效，已有库不会自动获得新约束（开发期可删库重建）。
DDL = """
-- 人设表：每个角色一行（妈妈、爸爸、小夏……）
CREATE TABLE IF NOT EXISTS persona (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL DEFAULT '陪伴者',
  relation      TEXT NOT NULL DEFAULT '朋友',
  traits        TEXT NOT NULL DEFAULT '[]',            -- JSON 数组：性格标签
  catchphrases  TEXT NOT NULL DEFAULT '[]',            -- JSON 数组：口头禅
  values_json   TEXT NOT NULL DEFAULT '[]',            -- JSON 数组；values 是 SQLite 关键字
  care_style    TEXT NOT NULL DEFAULT '',              -- 关心方式，单句描述
  system_prompt TEXT NOT NULL DEFAULT '',              -- 拼接后的 system prompt
  version       INTEGER NOT NULL DEFAULT 1,            -- 每次确认或修改 +1
  is_default    INTEGER NOT NULL DEFAULT 0
                CHECK (is_default IN (0, 1)),          -- 1 = 跳过配置生成的默认人设
  last_active_at TEXT,                                 -- 上次活跃时间，用于 time_context
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 长期记忆表：六类记忆统一存一张表，用 type 区分，避免多表语义漂移
CREATE TABLE IF NOT EXISTS memory_item (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id    INTEGER NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
  type          TEXT NOT NULL CHECK(type IN
                ('fact','preference','event','relationship','followup','summary')),
  content       TEXT NOT NULL,                         -- 记忆正文
  content_tok   TEXT NOT NULL,                         -- jieba 分词后空格连接，供 FTS5 索引
  importance    INTEGER NOT NULL DEFAULT 3 CHECK(importance BETWEEN 1 AND 5),
  -- shared：住址/工作/健康等基础事实，所有角色可见；personal：只属于当前角色
  scope         TEXT NOT NULL DEFAULT 'personal'
                CHECK(scope IN ('personal','shared')),
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','superseded','expired','completed')),
  superseded_by INTEGER REFERENCES memory_item(id) ON DELETE SET NULL,
  source_msg_id TEXT,                                  -- 来源消息 ID，用于溯源
  due_at        TEXT,                                  -- 仅 followup 使用，UTC 定长字符串
  notified_at   TEXT,                                  -- 主动消息已发送时间，防重复触发
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  last_hit_at   TEXT,                                  -- 最近一次被检索命中的时间
  hit_count     INTEGER NOT NULL DEFAULT 0             -- 被想起次数，侧栏展示用
);

-- 常用过滤条件建索引；due 索引只覆盖待跟进的 active 条目，减少索引体积
CREATE INDEX IF NOT EXISTS idx_mem_persona_status ON memory_item(persona_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_type_status ON memory_item(type, status);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memory_item(created_at);
CREATE INDEX IF NOT EXISTS idx_mem_due ON memory_item(due_at)
  WHERE type = 'followup' AND status = 'active';

-- 独立 FTS5 表：索引 content_tok（已分词文本），rowid 与 memory_item.id 对齐。
-- 不直接对 content 建索引，因为 SQLite 默认分词器对中文基本不可用。
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content_tok);

-- 删除记忆时同步删除 FTS 行；角色级联删除记忆时也会触发，避免孤儿索引
CREATE TRIGGER IF NOT EXISTS trg_memory_item_delete
AFTER DELETE ON memory_item
BEGIN
  DELETE FROM memory_fts WHERE rowid = old.id;
END;

-- 会话与角色绑定：一个 thread 属于一个角色，聊天时按它取 system_prompt
CREATE TABLE IF NOT EXISTS conversation (
  thread_id  TEXT PRIMARY KEY,
  persona_id INTEGER NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
  title      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_persona ON conversation(persona_id);

-- 主动消息通知表：前端轮询读取，read_at 为空表示未读
CREATE TABLE IF NOT EXISTS notifications (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  TEXT NOT NULL,
  memory_id  INTEGER REFERENCES memory_item(id) ON DELETE SET NULL,
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications(read_at, created_at);

-- 设置表：单行，id 固定为 1；开关与上限都用 CHECK 锁住取值范围
CREATE TABLE IF NOT EXISTS settings (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  proactive_enabled INTEGER NOT NULL DEFAULT 1
                    CHECK (proactive_enabled IN (0, 1)),   -- 主动关怀总开关
  quiet_start       TEXT NOT NULL DEFAULT '22:00',         -- 本地时区 HH:MM
  quiet_end         TEXT NOT NULL DEFAULT '08:00',
  daily_limit       INTEGER NOT NULL DEFAULT 1
                    CHECK (daily_limit BETWEEN 0 AND 5),   -- 每日主动消息上限
  low_mood_pause    INTEGER NOT NULL DEFAULT 1
                    CHECK (low_mood_pause IN (0, 1)),      -- 低落期自动暂停
  voice_name        TEXT NOT NULL DEFAULT 'zh-CN-XiaoxiaoNeural',
  auto_read         INTEGER NOT NULL DEFAULT 0
                    CHECK (auto_read IN (0, 1)),           -- 自动朗读回复
  asr_enabled       INTEGER NOT NULL DEFAULT 1
                    CHECK (asr_enabled IN (0, 1)),         -- 语音输入开关
  last_summarized_at TEXT,                                 -- 摘要水位，落库防重启后重复压缩
  updated_at        TEXT NOT NULL
);
"""


def utc_now() -> str:
    """统一存储 UTC ISO8601 定长字符串，保证字符串比较正确。

    格式示例：2026-11-24T10:00:00Z
    定长 20 字符，字典序即时间序，SQLite 里可直接做 <= 比较。
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def app_db_path() -> Path:
    """业务库路径。

    相对路径基于项目根目录解析，而不是进程当前目录，
    避免从其他目录启动时静默创建一份空数据库。
    """
    path = Path(get_settings().app_db_path)
    return path if path.is_absolute() else BASE_DIR / path


async def connect() -> aiosqlite.Connection:
    """打开连接并设置连接级 PRAGMA；调用方负责关闭。

    PRAGMA 是连接级设置，不会随数据库文件持久化，
    所以每个新连接都必须重新执行一遍。
    """
    path = app_db_path()
    # mkdir 是微秒级操作，不值得为它切线程
    path.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    conn = await aiosqlite.connect(path)
    try:
        conn.row_factory = aiosqlite.Row  # 查询结果可按列名取值

        # WAL：读写不互相阻塞；busy_timeout：锁等待上限；
        # foreign_keys：SQLite 默认关闭外键，必须显式打开才有级联删除。
        await conn.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;"
        )

        # 某些文件系统不支持 WAL，SQLite 会静默退回其他模式。
        # 这里主动校验一次，至少让日志里能看到真实状态。
        async with conn.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        mode = str(row[0]).lower() if row else "unknown"
        if mode != "wal":
            logger.warning("journal_mode 未启用 WAL，当前模式：%s", mode)
        return conn
    except Exception:
        # PRAGMA 失败时把连接关掉再抛，避免调用方拿不到 conn 而泄漏
        await conn.close()
        raise


async def init_db() -> bool:
    """幂等初始化：建表 + 确保 settings 有默认行。

    返回 True 表示检测到旧版单角色表并清空了数据（调用方据此清 checkpoint）。

    设置行只在首次创建时使用 .env 里的种子值；
    已存在时 OR IGNORE 跳过，不覆盖用户后来改过的配置。
    """
    settings = get_settings()
    # "22:00-08:00" → quiet_start="22:00", quiet_end="08:00"
    quiet_start, _, quiet_end = settings.proactive_default_quiet.partition("-")

    conn = await connect()
    migrated = False
    try:
        # 旧版 persona 带 CHECK(id=1)，无法容纳多个角色；按用户选择清空重建
        async with conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='persona'"
        ) as cursor:
            row = await cursor.fetchone()
        if row and "CHECK (id = 1)" in (row[0] or ""):
            migrated = True
            await conn.executescript(
                """
                DROP TABLE IF EXISTS notifications;
                DROP TABLE IF EXISTS memory_fts;
                DROP TABLE IF EXISTS conversation;
                DROP TABLE IF EXISTS memory_item;
                DROP TABLE IF EXISTS persona;
                """
            )
            logger.warning("legacy single-persona schema detected, rebuilding")

        await conn.executescript(DDL)  # executescript 会隐式提交

        # 清理历史遗留的孤儿 FTS 行（触发器上线前删除的数据）
        await conn.execute("DELETE FROM memory_fts WHERE rowid NOT IN (SELECT id FROM memory_item)")

        await conn.execute(
            """
            INSERT OR IGNORE INTO settings
              (id, quiet_start, quiet_end, daily_limit, updated_at)
            VALUES (1, ?, ?, ?, ?)
            """,
            (
                quiet_start.strip() or "22:00",
                quiet_end.strip() or "08:00",
                settings.proactive_daily_limit,
                utc_now(),
            ),
        )
        await conn.commit()
    finally:
        # 无论成功失败都关闭连接，避免文件句柄泄漏
        await conn.close()
    logger.info("database ready: %s", app_db_path())
    return migrated
