# -*- coding: utf-8 -*-
"""旧库迁移测试：验证启动迁移补齐 session_id / status / error_message / sessions 回填。"""
import os
import sqlite3
import tempfile
import uuid

# 1. 造一个旧 schema 的临时库（无 session_id / status / error_message / sessions / is_default）
tmp = os.path.join(tempfile.gettempdir(), f"ai_eval_migrate_{uuid.uuid4().hex[:8]}.db")
conn = sqlite3.connect(tmp)
conn.executescript(
    """
    CREATE TABLE evals (
        id TEXT PRIMARY KEY,
        eval_at TEXT NOT NULL,
        persona_name TEXT NOT NULL,
        skill_name TEXT NOT NULL,
        model TEXT NOT NULL,
        input_text TEXT NOT NULL,
        output_text TEXT NOT NULL,
        feedback TEXT DEFAULT NULL,
        feedback_at TEXT DEFAULT NULL
    );
    CREATE TABLE model_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '',
        port INTEGER DEFAULT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE personas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
)
conn.executemany(
    "INSERT INTO evals (id, eval_at, persona_name, skill_name, model, input_text, output_text) "
    "VALUES (?,?,?,?,?,?,?)",
    [
        ("old-1", "2026-08-01 10:00:00", "人设A", "SkillA", "m1", "第一条输入内容", '{"total_score": 5}'),
        ("old-2", "2026-08-01 10:01:00", "人设A", "SkillA", "m1", "第二条输入内容", ""),
        ("old-3", "2026-08-01 10:02:00", "人设B", "SkillB", "m2",
         "第三条输入内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长",
         '{"total_score": 1}'),
    ],
)
conn.commit()
conn.close()

# 2. 指向临时库并执行启动迁移
os.environ["AI_EVAL_DB"] = tmp
import server  # noqa: E402

server.init_db()

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:120] if extra else ""))
    if not cond:
        fails.append(name)


with server.db_conn() as c:
    rows = {r["id"]: r for r in c.execute("SELECT * FROM evals").fetchall()}
    check("completed migrated from non-empty output", rows["old-1"]["status"] == "completed", rows["old-1"]["status"])
    check("failed migrated from empty output", rows["old-2"]["status"] == "failed", rows["old-2"]["status"])
    check("failed error_message filled", "历史评估未完成" in (rows["old-2"]["error_message"] or ""),
          rows["old-2"]["error_message"])
    check("legacy session_id assigned", bool(rows["old-3"]["session_id"]), rows["old-3"]["session_id"])
    check("legacy NULL sessions isolated",
          rows["old-1"]["session_id"] != rows["old-2"]["session_id"] != rows["old-3"]["session_id"],
          (rows["old-1"]["session_id"], rows["old-2"]["session_id"], rows["old-3"]["session_id"]))

    sess = {r["session_id"]: r for r in c.execute("SELECT * FROM sessions").fetchall()}
    check("sessions backfilled count", len(sess) == 3, len(sess))
    check("session title from first input",
          sess[rows["old-1"]["session_id"]]["title"] == "第一条输入内容", sess[rows["old-1"]["session_id"]]["title"])
    check("session title truncated at 36",
          sess[rows["old-3"]["session_id"]]["title"].endswith("…"), sess[rows["old-3"]["session_id"]]["title"])
    check("session created_at backfilled", bool(sess[rows["old-1"]["session_id"]]["created_at"]))

    # is_default 锁定标记迁移
    pcols = [r[1] for r in c.execute("PRAGMA table_info(personas)").fetchall()]
    check("is_default column added", "is_default" in pcols, pcols)

# 3. 二次 init_db 幂等：迁移不重复破坏数据
server.init_db()
with server.db_conn() as c:
    n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    check("init_db idempotent sessions", n == 3, n)
    rows = {r["id"]: r for r in c.execute("SELECT * FROM evals").fetchall()}
    check("init_db idempotent status", rows["old-2"]["status"] == "failed", rows["old-2"]["status"])

try:
    os.remove(tmp)
except PermissionError:
    pass  # Windows 下连接句柄未释放时忽略清理，临时目录会自行回收

print()
print("TOTAL FAILURES:", len(fails), fails if fails else "")
raise SystemExit(1 if fails else 0)
