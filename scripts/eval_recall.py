"""S10 检索评估：Recall@5、MRR、噪音率、角色隔离。

零成本：只跑本地 FTS5 + jieba，不调用模型。
使用临时数据库，不污染 data/app.db。

数据集：30 条记忆（六类全覆盖）+ 20 个带标准答案的查询，分三组：
- hit：应命中的查询，算 Recall@5 / MRR / 噪音率；
- isolation：其他角色的 personal 记忆不应被检索到；
- shared：shared 事实应能被其他角色检索到。

用法（项目根目录）：
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/eval_recall.py
    PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/eval_recall.py -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须在导入 app.* 之前设置，确保用临时库
_TMP_DIR = tempfile.mkdtemp(prefix="niannian-eval-recall-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from app.db import connect, init_db, utc_now  # noqa: E402
from app.services.search import search, tokenize  # noqa: E402

# (key, persona_id, type, content, importance, scope)
# 角色：1=妈妈（主角色），2=小胖，3=表姐
MEMORIES = [
    ("home", 1, "fact", "用户住在杭州滨江区", 5, "shared"),
    ("job", 1, "fact", "用户在做产品运营", 4, "shared"),
    ("allergy", 1, "fact", "用户对花生过敏", 5, "shared"),
    ("cat", 1, "fact", "用户养了一只叫豆豆的猫", 4, "shared"),
    ("hometown", 1, "fact", "用户老家在山东济南", 3, "shared"),
    ("age", 1, "fact", "用户今年二十六岁", 2, "shared"),
    ("commute", 1, "fact", "用户每天通勤一小时", 2, "shared"),
    ("python", 1, "fact", "用户在做 Python 项目", 3, "shared"),
    ("milk", 1, "preference", "用户喜欢喝热牛奶", 3, "personal"),
    ("cilantro", 1, "preference", "用户不爱吃香菜", 3, "personal"),
    ("music", 1, "preference", "用户睡前喜欢听轻音乐", 3, "personal"),
    ("drama", 1, "preference", "用户喜欢看悬疑剧", 2, "personal"),
    ("alcohol", 1, "preference", "用户不太喝酒", 2, "personal"),
    ("sleep", 1, "preference", "用户经常熬夜到一两点", 3, "personal"),
    ("interview", 1, "event", "用户下周三要去面试", 5, "personal"),
    ("anemia", 1, "event", "用户上周体检报告有点贫血", 4, "personal"),
    ("trip", 1, "event", "用户下个月要出差广州", 3, "personal"),
    ("mom", 1, "relationship", "用户和妈妈关系很好，每周视频", 4, "personal"),
    ("dinner", 1, "followup", "答应妈妈周末回家吃饭", 4, "personal"),
    ("iced", 2, "preference", "用户喜欢喝冰美式", 3, "personal"),
    ("ball", 2, "event", "用户周日约了小胖打球", 4, "personal"),
    ("roommate", 2, "relationship", "用户和小胖是大学室友", 4, "personal"),
    ("game", 2, "followup", "答应小胖推荐一款游戏", 3, "personal"),
    ("support", 2, "preference", "用户打游戏喜欢玩辅助", 2, "personal"),
    ("concert", 2, "event", "用户上个月和小胖去看了演唱会", 3, "personal"),
    ("morning", 2, "preference", "用户讨厌早起", 2, "personal"),
    ("overtime", 2, "event", "用户上周加班到很晚", 3, "personal"),
    ("cat3y", 2, "event", "用户养猫三年了", 2, "personal"),
    ("sweet", 3, "preference", "用户喜欢偏甜的口味", 2, "personal"),
    ("gift", 3, "followup", "答应给同事带特产", 2, "personal"),
]

# (group, query, persona_id, expected_keys, forbidden_keys)
QUERIES = [
    ("hit", "住在哪里", 1, ["home"], []),
    ("hit", "做什么工作", 1, ["job"], []),
    ("hit", "花生过敏", 1, ["allergy"], []),
    ("hit", "养的猫", 1, ["cat"], []),
    ("hit", "老家", 1, ["hometown"], []),
    ("hit", "多少岁", 1, ["age"], []),
    ("hit", "通勤", 1, ["commute"], []),
    ("hit", "Python 项目", 1, ["python"], []),
    ("hit", "热牛奶", 1, ["milk"], []),
    ("hit", "香菜", 1, ["cilantro"], []),
    ("hit", "面试", 1, ["interview"], []),
    ("hit", "体检", 1, ["anemia"], []),
    ("hit", "每周视频", 1, ["mom"], []),
    ("hit", "回家吃饭", 1, ["dinner"], []),
    ("hit", "悬疑剧", 1, ["drama"], []),
    ("isolation", "冰美式", 1, [], ["iced"]),
    ("isolation", "打球", 1, [], ["ball"]),
    ("isolation", "大学室友", 1, [], ["roommate"]),
    ("shared", "住在哪里", 2, ["home"], []),
    ("shared", "花生过敏", 2, ["allergy"], []),
]


async def seed() -> dict[str, int]:
    """建库并写入角色与标注记忆，返回 key → memory_id 映射。"""
    await init_db()
    conn = await connect()
    try:
        now = utc_now()
        for persona_id, name in ((1, "妈妈"), (2, "小胖"), (3, "表姐")):
            await conn.execute(
                "INSERT INTO persona (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (persona_id, name, now, now),
            )

        key_to_id: dict[str, int] = {}
        for key, persona_id, type_, content, importance, scope in MEMORIES:
            cursor = await conn.execute(
                """
                INSERT INTO memory_item
                  (persona_id, type, content, content_tok, importance, scope, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (persona_id, type_, content, tokenize(content), importance, scope, now, now),
            )
            memory_id = cursor.lastrowid
            await conn.execute(
                "INSERT INTO memory_fts(rowid, content_tok) VALUES (?, ?)",
                (memory_id, tokenize(content)),
            )
            key_to_id[key] = memory_id
        await conn.commit()
        return key_to_id
    finally:
        await conn.close()


async def evaluate(key_to_id: dict[str, int], verbose: bool) -> None:
    hit_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    noise_rates: list[float] = []
    isolation_pass = 0
    isolation_total = 0
    shared_pass = 0
    shared_total = 0

    for group, query, persona_id, expected_keys, forbidden_keys in QUERIES:
        hits = await search(persona_id=persona_id, queries=[query], k=5, update_hits=False)
        returned = [item["id"] for item in hits]
        expected = {key_to_id[key] for key in expected_keys}
        forbidden = {key_to_id[key] for key in forbidden_keys}

        if group == "hit":
            found = expected & set(returned)
            recall = len(found) / len(expected) if expected else 0.0
            hit_recalls.append(recall)

            # 第一个应命中项在结果中的名次
            rank = next((index + 1 for index, mid in enumerate(returned) if mid in expected), None)
            reciprocal_ranks.append(1 / rank if rank else 0.0)

            noise = (len(returned) - len(found)) / len(returned) if returned else 0.0
            noise_rates.append(noise)

            status = "OK  " if found else "FAIL"
            print(
                f"{status} [hit] {query:10s} 命中 {len(found)}/{len(expected)} "
                f"名次={rank or '-'} 返回={len(returned)} 噪音={noise:.0%}"
            )
        elif group == "isolation":
            isolation_total += 1
            leaked = forbidden & set(returned)
            if not leaked:
                isolation_pass += 1
            print(
                f"{'OK  ' if not leaked else 'FAIL'} [iso] {query:10s} "
                f"返回={len(returned)} 泄漏={len(leaked)}"
            )
        else:
            shared_total += 1
            found = expected & set(returned)
            if found == expected:
                shared_pass += 1
            print(
                f"{'OK  ' if found == expected else 'FAIL'} [shr] {query:10s} "
                f"命中 {len(found)}/{len(expected)}"
            )

        if verbose and hits:
            for index, item in enumerate(hits, start=1):
                mark = "*" if item["id"] in expected else " "
                print(
                    f"      {mark}{index}. [{item['type']}] {item['content']} "
                    f"(score={item['score']})"
                )

    print()
    print("--- 汇总 ---")
    mean = lambda values: sum(values) / len(values) if values else 0.0  # noqa: E731
    print(
        f"查询总数        {len(QUERIES)}"
        f"（hit {len(hit_recalls)} / isolation {isolation_total} / shared {shared_total}）"
    )
    print(f"Recall@5        {mean(hit_recalls):.1%}")
    print(f"MRR             {mean(reciprocal_ranks):.3f}")
    print(f"平均噪音率      {mean(noise_rates):.1%}")
    print(f"角色隔离        {isolation_pass}/{isolation_total} 通过")
    print(f"共享可见        {shared_pass}/{shared_total} 通过")


async def main() -> None:
    verbose = "-v" in sys.argv
    try:
        key_to_id = await seed()
        await evaluate(key_to_id, verbose)
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
