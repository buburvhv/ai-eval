"""AI 辅助评估工具 - 后端入口。运行: python server.py"""
import csv
import io
import os
import shutil
import sys
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlsplit

IS_FROZEN = getattr(sys, "frozen", False)

# 路径策略：
# - 源码/打包内置资源（前端 static）：源码模式取当前目录；exe 模式取 PyInstaller 解包目录(_MEIPASS)
# - 数据（data.db、skills 上传目录）：exe 模式放 exe 同级目录，可写、跨运行持久化；源码模式即项目目录
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else BASE_DIR
DB_PATH = Path(os.environ.get("AI_EVAL_DB", DATA_DIR / "data.db"))
SKILL_DIR = DATA_DIR / "skills"
STATIC_DIR = BASE_DIR / "static"

# 模型网关常用端口，探测顺序按出现先后，用户可用“key;port”指定端口
DEFAULT_PORTS = [80, 8080, 8000, 11434, 443]  # http 默认探测顺序

EVAL_SYSTEM_PROMPT = """你是一名专业的模型输出质量评估员。请严格按照用户消息中提供的《评估标准》（skill）对「用户人设」与「待评估对话」进行评估。

重要：如果《评估标准》中定义了「输出JSON格式」或「输出规则」，则最终结果必须严格按其定义的 JSON 结构输出，字段名、维度顺序、取值规则完全遵循标准，不得增删字段、不得输出 Markdown 代码块标记或任何 JSON 以外的文字。
若标准未定义输出格式，则按以下结构输出纯 JSON：
{"overall_issue": "...", "dimensions": [{"name": "...", "score": 0, "max_score": 2, "issue": ""}], "total_score": 0, "max_score": 0, "main_issues": [], "suggestions": []}
"""


# ---------- 数据库 ----------
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as c:
        c.executescript(

            """
            CREATE TABLE IF NOT EXISTS model_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                port INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evals (
                id TEXT PRIMARY KEY,
                session_id TEXT DEFAULT NULL,
                eval_at TEXT NOT NULL,
                persona_name TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                model TEXT NOT NULL,
                input_text TEXT NOT NULL,
                output_text TEXT NOT NULL,
                feedback TEXT DEFAULT NULL,
                feedback_at TEXT DEFAULT NULL
            );
            """
        )
        # 旧库迁移：补 session_id 列
        cols = [r[1] for r in c.execute("PRAGMA table_info(evals)").fetchall()]
        if "session_id" not in cols:
            c.execute("ALTER TABLE evals ADD COLUMN session_id TEXT DEFAULT NULL")
        # 旧库迁移：is_default 锁定标记。存量数据视为作者内置默认配置，对所有用户只读。
        for table in ("model_config", "personas", "skills"):
            tcols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if "is_default" not in tcols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
                c.execute(f"UPDATE {table} SET is_default=1")
        c.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # exe 首次运行：exe 同级还没有数据时，用打包内置的模板库初始化（开箱即有预置模型地址/人设/Skill）
    if IS_FROZEN:
        if not DB_PATH.exists():
            tpl = BASE_DIR / "data.db.template"
            if tpl.exists():
                shutil.copy2(tpl, DB_PATH)
        if not SKILL_DIR.exists():
            pkg_skills = BASE_DIR / "skills"
            if pkg_skills.exists():
                shutil.copytree(pkg_skills, SKILL_DIR)
    init_db()
    SKILL_DIR.mkdir(exist_ok=True)
    yield


app = FastAPI(title="AI 辅助评估工具", lifespan=lifespan)


# ---------- 模型网关探测（通用 OpenAI 兼容接口） ----------
def normalize_base(raw: str) -> str:
    """把用户输入规范成带 scheme 的 host[:port] 形式。兼容 'host:port' 与 'http://host:port/path'。"""
    s = raw.strip()
    if "://" not in s:
        s = "http://" + s
    return s.rstrip("/")


def _candidate_urls(base: str, port: int | None) -> list[str]:
    """生成候选 base_url，依次尝试。探测原则：先做 GET /models 按内容判断，失败再按 OpenAI 错误格式兜底。"""
    p = urlsplit(base)
    scheme = p.scheme or "http"
    host = p.netloc or p.path
    path = p.path if p.netloc else ""
    has_port = ":" in host
    urls: list[str] = []
    if port and not has_port:
        # 指定端口：尊重已有路径，缺省补 /v1（常见网关路径）
        if path and path != "/":
            urls.append(f"{scheme}://{host}:{port}{path}")
        urls.append(f"{scheme}://{host}:{port}/v1")
    elif has_port:
        # base_url 已带端口，直接使用
        if path and path != "/":
            urls.append(f"{scheme}://{host}{path}")
        urls.append(f"{scheme}://{host}/v1")
    else:
        for pp in DEFAULT_PORTS:
            addr = f"{host}:{pp}"
            if path and path != "/":
                urls.append(f"{scheme}://{addr}{path}")
            urls.append(f"{scheme}://{addr}/v1")
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _parse_models_payload(data) -> list[dict]:
    """从 GET /models 响应中提取模型 id 列表，兼容多种格式。"""
    models: list[dict] = []
    if isinstance(data, dict):
        arr = data.get("data") or data.get("models") or data.get("model_list")
        if arr is None:
            # 某些网关直接以模型名为 key
            for k, v in data.items():
                if isinstance(v, dict) and "id" in v:
                    arr = list(data.values())
                    break
        if isinstance(arr, list):
            models = [m for m in arr if isinstance(m, dict)]
    elif isinstance(data, list):
        models = [m for m in data if isinstance(m, dict)]
    return models


def _extract_model_ids(models: list[dict]) -> list[str]:
    ids: list[str] = []
    for m in models:
        mid = m.get("id") or m.get("name") or m.get("model") or m.get("model_name")
        if mid and str(mid).strip():
            ids.append(str(mid).strip())
    return ids


def _looks_like_openai_error(status: int, body: str) -> bool:
    return '"error"' in body or status in (401, 404)


async def probe_models(base_url: str, api_key: str, port: int | None, timeout: float = 5.0, total_timeout: float = 15.0) -> tuple[str, list[str]]:
    """探测网关并返回 (实际生效的 base_url, 模型 id 列表)。失败抛 HTTPException。
    候选地址按优先级顺序探测：单次请求超时 timeout，总时长上限 total_timeout——
    避免地址不可达时长时间卡住（此前最坏 10 个候选 × 8s = 80s）。"""
    base_url = normalize_base(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_err = "无法连接到模型网关"

    async def try_get(client: httpx.AsyncClient, url: str, t: float) -> tuple[bool, object]:
        try:
            r = await client.get(f"{url}/models", headers=headers, timeout=t)
        except httpx.HTTPError:
            return False, None
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = None
            models = _parse_models_payload(data)
            ids = _extract_model_ids(models)
            if ids:
                return True, ids
            if isinstance(data, list) and data and all(isinstance(x, str) for x in data):
                return True, data
            # 200 但拿不到模型列表：尝试 /v1/models
            try:
                r2 = await client.get(f"{url}/v1/models", headers=headers, timeout=t)
                if r2.status_code == 200:
                    data2 = r2.json()
                    ids2 = _extract_model_ids(_parse_models_payload(data2))
                    if ids2:
                        return True, ids2
            except (httpx.HTTPError, ValueError):
                pass
            return True, []
        elif _looks_like_openai_error(r.status_code, r.text):
            return True, []  # 网关活着（OpenAI 兼容），只是 /models 未开放或无模型
        return False, None

    deadline = time.monotonic() + total_timeout
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        for url in _candidate_urls(base_url, port):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ok, result = await try_get(client, url, min(timeout, remaining))
            if ok:
                return url, result

    raise HTTPException(
        status_code=502,
        detail=f"模型网关探测失败：{last_err}。请检查 base_url、端口或 API Key 是否正确。",
    )


async def call_model(base_url: str, api_key: str, model: str, messages: list[dict], timeout: float = 900.0) -> str:
    """调用 OpenAI 兼容的 chat/completions 接口，返回纯文本回复。
    部分模型（如 glm5.3 flash）生成评估报告较慢，默认超时放宽到 900s。"""
    base_url = normalize_base(base_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"model": model, "messages": messages, "temperature": 0.2, "stream": False}
    async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
        r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if r.status_code != 200:
            detail = r.text[:500] or f"HTTP {r.status_code}"
            raise HTTPException(status_code=502, detail=f"模型调用失败：{detail}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail=f"模型响应格式异常：{str(data)[:300]}")


# ---------- 通用小工具 ----------
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_model_config(payload: dict) -> tuple[str, str, int | None]:
    """从请求里解析模型配置：请求中带了 base_url 就用请求的（用户自定义配置只存浏览器，服务端不落库），否则用默认配置。"""
    base = (payload.get("base_url") or "").strip()
    key = payload.get("api_key") or ""
    port = payload.get("port") or None
    if base:
        return normalize_base(base), key, port
    with db_conn() as c:
        row = c.execute(
            "SELECT * FROM model_config WHERE is_default=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="尚未配置模型网关，请先在设置中填写 Base URL 和 API Key。")
    return row["base_url"], row["api_key"], row["port"]


# ---------- Pydantic 模型 ----------
class ModelConfigIn(BaseModel):
    base_url: str
    api_key: str = ""
    port: int | None = None


class ModelProbeIn(BaseModel):
    """宽松版：/api/model/models 允许空 body（回退默认配置）。"""
    base_url: str | None = None
    api_key: str | None = None
    port: int | None = None


class TestChatIn(BaseModel):
    model: str
    base_url: str | None = None
    api_key: str | None = None
    port: int | None = None


class PersonaIn(BaseModel):
    name: str
    content: str


class SkillIn(BaseModel):
    name: str


class EvalIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    port: int | None = None
    model: str
    persona_id: int | None = None
    persona_text: str | None = None
    skill_id: int | None = None
    skill_text: str | None = None
    input_text: str
    session_id: str | None = None  # 为空则新建会话


class FeedbackIn(BaseModel):
    feedback: str  # correct / incorrect


# ---------- 模型配置 ----------
@app.post("/api/model/test")
async def api_model_test(cfg: ModelConfigIn):
    """探测网关并返回模型列表（纯探测，不落库）。默认配置由作者预置在库中，用户自定义配置只存浏览器本地。"""
    try:
        base, models = await probe_models(cfg.base_url.strip(), cfg.api_key, cfg.port)
    except HTTPException as e:
        raise HTTPException(status_code=400, detail=e.detail)
    if not models:
        raise HTTPException(
            status_code=400,
            detail="网关连接成功，但 /models 接口未返回模型列表（可能未开放该接口或暂无可用模型）。",
        )
    return {"base_url": base, "models": models}


@app.post("/api/seed-default-config")
async def api_seed_default_config(cfg: ModelConfigIn):
    """写入/更新作者默认模型配置（部署时初始化一次；已存在默认配置时更新其内容）。
    先探测网关，落库存探测成功后的完整 base_url（与调用时使用的地址一致）。"""
    try:
        base, _models = await probe_models(cfg.base_url.strip(), cfg.api_key, cfg.port)
    except HTTPException as e:
        raise HTTPException(status_code=400, detail=e.detail)
    with db_conn() as c:
        row = c.execute("SELECT id FROM model_config WHERE is_default=1 ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            c.execute(
                "UPDATE model_config SET base_url=?, api_key=?, port=? WHERE id=?",
                (base, cfg.api_key, cfg.port, row["id"]),
            )
        else:
            c.execute(
                "INSERT INTO model_config (base_url, api_key, port, created_at, is_default) VALUES (?,?,?,?,1)",
                (base, cfg.api_key, cfg.port, now_str()),
            )
        c.commit()
    return {"ok": True, "base_url": base}


@app.get("/api/model/config")
def api_model_config():
    """返回默认（作者内置）配置。用户自定义配置仅存于浏览器本地，不经此接口。"""
    with db_conn() as c:
        row = c.execute(
            "SELECT * FROM model_config WHERE is_default=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {"configured": False}
    return {"configured": True, "base_url": row["base_url"], "api_key": row["api_key"], "port": row["port"]}


@app.post("/api/model/models")
async def api_model_models(cfg: ModelProbeIn | None = None):
    """按请求携带的配置探测模型列表（用户自定义配置存浏览器本地）；无请求配置时回退默认配置。"""
    if cfg and cfg.base_url and cfg.base_url.strip():
        base, key, port = normalize_base(cfg.base_url.strip()), cfg.api_key, cfg.port
    else:
        with db_conn() as c:
            row = c.execute(
                "SELECT * FROM model_config WHERE is_default=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="尚未配置模型网关。")
        base, key, port = row["base_url"], row["api_key"], row["port"]
    try:
        _, models = await probe_models(base, key, port)
    except HTTPException as e:
        raise HTTPException(status_code=502, detail=e.detail)
    return {"models": models}


@app.post("/api/model/test-chat")
async def api_model_test_chat(cfg: TestChatIn):
    """用“ping”验证模型真的可调用。请求带配置用请求的（用户自定义），否则用默认配置。"""
    if cfg.base_url and cfg.base_url.strip():
        base, key = normalize_base(cfg.base_url.strip()), cfg.api_key or ""
    else:
        with db_conn() as c:
            row = c.execute(
                "SELECT * FROM model_config WHERE is_default=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="尚未配置模型网关。")
        base, key = row["base_url"], row["api_key"]
    reply = await call_model(
        base, key, cfg.model,
        [{"role": "user", "content": "请只回复两个字：收到"}],
        timeout=30.0,
    )
    return {"reply": reply[:200]}


# ---------- 人设 ----------
@app.get("/api/personas")
def list_personas():
    with db_conn() as c:
        rows = c.execute(
            "SELECT id, name, content, created_at, updated_at, is_default FROM personas ORDER BY is_default DESC, id ASC"
        ).fetchall()
    return {"personas": [dict(r) for r in rows]}


@app.post("/api/personas")
def create_persona(p: PersonaIn):
    name, content = p.name.strip(), p.content.strip()
    if not name or not content:
        raise HTTPException(status_code=400, detail="人设名称和内容不能为空。")
    ts = now_str()
    with db_conn() as c:
        try:
            c.execute(
                "INSERT INTO personas (name, content, created_at, updated_at, is_default) VALUES (?,?,?,?,0)",
                (name, content, ts, ts),
            )
            c.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail=f"人设名称「{name}」已存在，请换一个名称。")
    return {"ok": True}


def _ensure_not_default(conn: sqlite3.Connection, table: str, pid: int, label: str) -> None:
    row = conn.execute(f"SELECT is_default FROM {table} WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"{label}不存在。")
    if row["is_default"]:
        raise HTTPException(status_code=403, detail=f"内置默认{label}不可修改或删除。")


@app.put("/api/personas/{pid}")
def update_persona(pid: int, p: PersonaIn):
    name, content = p.name.strip(), p.content.strip()
    if not name or not content:
        raise HTTPException(status_code=400, detail="人设名称和内容不能为空。")
    with db_conn() as c:
        _ensure_not_default(c, "personas", pid, "人设")
        cur = c.execute(
            "UPDATE personas SET name=?, content=?, updated_at=? WHERE id=? AND is_default=0",
            (name, content, now_str(), pid),
        )
        c.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="人设不存在。")
    return {"ok": True}


@app.delete("/api/personas/{pid}")
def delete_persona(pid: int):
    with db_conn() as c:
        _ensure_not_default(c, "personas", pid, "人设")
        cur = c.execute("DELETE FROM personas WHERE id=? AND is_default=0", (pid,))
        c.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="人设不存在。")
    return {"ok": True}


# ---------- Skill（评估标准） ----------
@app.get("/api/skills")
def list_skills():
    with db_conn() as c:
        rows = c.execute(
            "SELECT id, name, filename, content, created_at, updated_at, is_default "
            "FROM skills ORDER BY is_default DESC, id ASC"
        ).fetchall()
    return {"skills": [dict(r) for r in rows]}


@app.get("/api/skills/{sid}")
def get_skill(sid: int):
    with db_conn() as c:
        row = c.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Skill 不存在。")
    return dict(row)


@app.post("/api/skills")
async def create_skill(name: str = Form(...), file: UploadFile = File(...)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill 名称不能为空。")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空。")
    content = raw.decode("utf-8", errors="replace")
    fname = file.filename or f"{name}.md"
    safe_stem = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name) or "skill"
    fname = f"{safe_stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{Path(fname).suffix or '.md'}"
    fpath = SKILL_DIR / fname
    fpath.write_bytes(raw)
    ts = now_str()
    with db_conn() as c:
        try:
            c.execute(
                "INSERT INTO skills (name, filename, filepath, content, created_at, updated_at, is_default) "
                "VALUES (?,?,?,?,?,?,0)",
                (name, fname, str(fpath), content, ts, ts),
            )
            c.commit()
        except sqlite3.IntegrityError:
            fpath.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Skill 名称「{name}」已存在，请换一个名称。")
    return {"ok": True}


@app.delete("/api/skills/{sid}")
def delete_skill(sid: int):
    with db_conn() as c:
        _ensure_not_default(c, "skills", sid, "Skill")
        row = c.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Skill 不存在。")
        c.execute("DELETE FROM skills WHERE id=? AND is_default=0", (sid,))
        c.commit()
    Path(row["filepath"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------- 评估 ----------
@app.post("/api/eval")
async def api_eval(p: EvalIn):
    input_text = p.input_text.strip()
    if not input_text:
        raise HTTPException(status_code=400, detail="待评估信息不能为空。")

    persona_text = ""
    persona_name = "（未提供人设）"
    if p.persona_id:
        with db_conn() as c:
            row = c.execute("SELECT name, content FROM personas WHERE id=?", (p.persona_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="所选人设不存在。")
        persona_text = row["content"]
        persona_name = row["name"]
    elif p.persona_text and p.persona_text.strip():
        persona_text = p.persona_text.strip()
        persona_name = "（临时人设）"

    skill_text = ""
    skill_name = ""
    if p.skill_id:
        with db_conn() as c:
            row = c.execute("SELECT name, content FROM skills WHERE id=?", (p.skill_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="所选 Skill 不存在。")
        skill_text = row["content"]
        skill_name = row["name"]
    elif p.skill_text and p.skill_text.strip():
        skill_text = p.skill_text.strip()
        skill_name = "（临时Skill）"
    if not skill_text:
        raise HTTPException(status_code=400, detail="请选择或上传评估标准（Skill）。")

    base, key, port = _resolve_model_config(p.model_dump())

    user_content = (
        f"【评估标准（skill）】\n{skill_text}\n\n"
        f"【用户人设】\n{persona_text or '（未提供）'}\n\n"
        f"【待评估信息】\n{input_text}"
    )
    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    # 先落库占位（output 为空）：用户发送即创建会话历史，不必等模型返回
    eval_id = uuid.uuid4().hex
    session_id = p.session_id or uuid.uuid4().hex
    eval_at = now_str()
    with db_conn() as c:
        c.execute(
            "INSERT INTO evals (id, session_id, eval_at, persona_name, skill_name, model, input_text, output_text) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eval_id, session_id, eval_at, persona_name, skill_name, p.model, input_text, ""),
        )
        c.commit()

    output = await call_model(base, key, p.model, messages)
    with db_conn() as c:
        cur = c.execute(
            "UPDATE evals SET output_text=? WHERE id=?", (output, eval_id)
        )
        c.commit()
        if cur.rowcount == 0:  # 占位记录被外部清理：重新写入完整记录，保证历史不丢
            c.execute(
                "INSERT INTO evals (id, session_id, eval_at, persona_name, skill_name, model, input_text, output_text) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (eval_id, session_id, eval_at, persona_name, skill_name, p.model, input_text, output),
            )
            c.commit()
    return {
        "id": eval_id,
        "session_id": session_id,
        "eval_at": eval_at,
        "persona_name": persona_name,
        "skill_name": skill_name,
        "output": output,
    }


# ---------- 反馈（点赞/点踩） ----------
@app.post("/api/eval/{eid}/feedback")
def api_feedback(eid: str, f: FeedbackIn):
    if f.feedback not in ("correct", "incorrect"):
        raise HTTPException(status_code=400, detail="feedback 只能是 correct 或 incorrect。")
    with db_conn() as c:
        row = c.execute("SELECT feedback FROM evals WHERE id=?", (eid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="评估记录不存在。")
        saved = row["feedback"] or f.feedback  # 已有标注则保持不变（不可覆盖）
        if row["feedback"] is None:
            c.execute("UPDATE evals SET feedback=?, feedback_at=? WHERE id=?", (saved, now_str(), eid))
        c.commit()
    return {"ok": True, "feedback": saved}


@app.get("/api/sessions")
def list_sessions():
    """按会话分组的评估历史，用于左侧栏。"""
    with db_conn() as c:
        rows = c.execute(
            """
            SELECT session_id,
                   MIN(eval_at) AS start_at,
                   COUNT(*) AS total,
                   SUM(CASE WHEN feedback='correct' THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN feedback='incorrect' THEN 1 ELSE 0 END) AS incorrect,
                   MIN(model) AS model,
                   MIN(skill_name) AS skill_name
            FROM evals
            GROUP BY session_id
            ORDER BY start_at DESC
            LIMIT 200
            """
        ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/sessions/{sid}/evals")
def session_evals(sid: str):
    with db_conn() as c:
        rows = c.execute(
            "SELECT * FROM evals WHERE session_id=? ORDER BY eval_at ASC", (sid,)
        ).fetchall()
    return {"evals": [dict(r) for r in rows]}


@app.get("/api/evals")
def list_evals():
    with db_conn() as c:
        rows = c.execute("SELECT * FROM evals ORDER BY eval_at DESC LIMIT 200").fetchall()
    return {"evals": [dict(r) for r in rows]}


@app.get("/api/evals/stats")
def evals_stats():
    with db_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM evals").fetchone()[0]
        correct = c.execute("SELECT COUNT(*) FROM evals WHERE feedback='correct'").fetchone()[0]
        incorrect = c.execute("SELECT COUNT(*) FROM evals WHERE feedback='incorrect'").fetchone()[0]
    return {"total": total, "correct": correct, "incorrect": incorrect}


@app.get("/api/evals/export")
def export_evals():
    """导出全部评估记录及人工标注为 CSV，方便线下查看标注数据。"""
    with db_conn() as c:
        rows = c.execute(
            "SELECT id, session_id, eval_at, persona_name, skill_name, model, "
            "input_text, output_text, feedback, feedback_at "
            "FROM evals ORDER BY eval_at DESC"
        ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["评估ID", "会话ID", "评估时间", "人设", "Skill", "模型",
         "待评估内容", "评估结果", "人工标注", "标注时间"]
    )
    fb_map = {"correct": "正确", "incorrect": "不正确"}
    for r in rows:
        writer.writerow([
            r["id"], r["session_id"] or "", r["eval_at"], r["persona_name"], r["skill_name"],
            r["model"], r["input_text"], r["output_text"],
            fb_map.get(r["feedback"] or "", "未标注"), r["feedback_at"] or "",
        ])
    csv_text = "﻿" + buf.getvalue()  # BOM：Excel 直接打开不乱码
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=eval_feedback.csv"},
    )


# ---------- 前端 ----------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import threading
    import time
    import webbrowser
    import uvicorn

    # 端口可用环境变量 AI_EVAL_PORT 覆盖（打包测试等场景）；默认 8790
    port = int(os.environ.get("AI_EVAL_PORT", "8790"))

    if IS_FROZEN:
        # exe 模式：启动后自动打开浏览器；控制台保留，Ctrl+C / 关闭窗口即退出
        def _open_browser() -> None:
            time.sleep(1.5)
            webbrowser.open(f"http://127.0.0.1:{port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    # 监听 0.0.0.0：本机开发用 127.0.0.1 访问，部署到内网/POPO 时其他机器才能访问
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
