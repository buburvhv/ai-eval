# -*- coding: utf-8 -*-
"""端到端冒烟测试：起一个 mock 模型网关，验证各 API 流程"""
import json
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- mock 模型网关（OpenAI 兼容：GET /v1/models + POST /v1/chat/completions）----
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/models", "/v1/models"):
            self._json({"data": [{"id": "mock-model-a"}, {"id": "mock-model-b"}]})
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        # 读掉请求体，避免客户端 ReadError
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if self.path in ("/chat/completions", "/v1/chat/completions"):
            # 模拟 SKILL.md 约定的六维 JSON 输出（含围栏与前后杂讯，模拟真实模型行为）
            payload = {
                "overall_issue": "整体缺乏真人感和人设引入，推进过于生硬",
                "dimensions": [
                    {"name": "意图识别", "score": 1, "max_score": 2, "issue": "第一轮未正确识别用户意图"},
                    {"name": "内容价值", "score": 2, "max_score": 2, "issue": ""},
                    {"name": "情绪价值", "score": 0, "max_score": 2, "issue": "第二轮否定用户感受，共情不足"},
                    {"name": "真人感", "score": 1, "max_score": 2, "issue": "口语感弱，像在背书"},
                    {"name": "人设匹配度", "score": 2, "max_score": 2, "issue": ""},
                    {"name": "自然延展", "score": 2, "max_score": 2, "issue": ""},
                ],
                "total_score": 99,
                "max_score": 12,
                "main_issues": [
                    {"dimension": "意图识别", "problem_type": "意图偏离", "severity": 1,
                     "round": 1, "evidence": "用户询问烦恼模型只回应'遇到什么事'", "description": "第1轮未定位用户主意图"},
                    {"dimension": "情绪价值", "problem_type": "否定用户感受", "severity": 2,
                     "round": 2, "evidence": "用户说不想失去他，模型回应'这根本不是啥大事'", "description": "第2轮直接否定用户感受"},
                ],
                "suggestions": ["先共情再推进对话", "减少连珠炮式追问，一次只问一个问题"],
            }
            content = "评估说明（杂讯）...\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n（完）"
            self._json({"choices": [{"message": {"role": "assistant", "content": content}}]})
        else:
            self._json({"error": {"message": "not found"}}, 404)


srv = ThreadingHTTPServer(("127.0.0.1", 18081), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

BASE = "http://127.0.0.1:8790"


def call(method, path, obj=None, timeout=60):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:150] if extra else ""))
    if not cond:
        fails.append(name)


# 1. 配置模型（指定端口 18081，走网关探测；探测不落库）
s, d = call("POST", "/api/model/test", {"base_url": "127.0.0.1", "api_key": "sk-test", "port": 18081})
check("model/test", s == 200 and d.get("models") == ["mock-model-a", "mock-model-b"], d)

# 2. 模型配置回读 + 模型列表（config 需先经 seed 一步写入默认配置）
s, d = call("POST", "/api/seed-default-config", {"base_url": "127.0.0.1", "api_key": "sk-test", "port": 18081})
check("seed default config", s == 200, d)
s, d = call("GET", "/api/model/config")
check("model/config default", s == 200 and d.get("configured") and d.get("port") == 18081, d)
s, d = call("POST", "/api/model/models", {})
check("model/models fallback default", s == 200 and len(d.get("models", [])) == 2, d)
s, d = call("POST", "/api/model/models", {"base_url": "127.0.0.1", "api_key": "sk-test", "port": 18081})
check("model/models with user cfg", s == 200 and len(d.get("models", [])) == 2, d)

# 2.1 默认配置不可被用户改动误伤：探测接口不再写库
s, d = call("GET", "/api/evals/stats")
check("stats reachable", s == 200, d)

# 3. 测试调用
s, d = call("POST", "/api/model/test-chat", {"model": "mock-model-a"})
check("model/test-chat", s == 200 and isinstance(d.get("reply"), str) and len(d["reply"]) > 0, d)

# 4. 人设 CRUD
import uuid

pname = f"测试客服{uuid.uuid4().hex[:6]}"
s, d = call("POST", "/api/personas", {"name": pname, "content": "你是一名耐心的客服"})
check("persona create", s == 200)
s, d = call("GET", "/api/personas")
pid = d["personas"][0]["id"] if s == 200 else 0
check("persona list", s == 200 and len(d["personas"]) >= 1)
s, d = call("PUT", f"/api/personas/{pid}", {"name": pname, "content": "你是一名耐心的客服 v2"})
check("persona update", s == 200, d)

# 5. Skill 上传（multipart）
import io

sname = f"客服评估标准{uuid.uuid4().hex[:6]}"

boundary = "----testboundary"
filename = "eval_std_test.md"
file_content = "# 评估标准\n1. 回复必须包含问候语\n2. 不允许出现敏感词"
body = io.BytesIO()
body.write(f"--{boundary}\r\n".encode())
body.write(f'Content-Disposition: form-data; name="name"\r\n\r\n{sname}\r\n'.encode())
body.write(f"--{boundary}\r\n".encode())
body.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
body.write(b"Content-Type: text/markdown\r\n\r\n")
body.write(file_content.encode())
body.write(f"\r\n--{boundary}--\r\n".encode())
req = urllib.request.Request(BASE + "/api/skills", data=body.getvalue(), method="POST",
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
with urllib.request.urlopen(req, timeout=30) as r:
    s, d = r.status, json.loads(r.read().decode())
check("skill upload", s == 200, d)
s, d = call("GET", "/api/skills")
sid = d["skills"][0]["id"] if s == 200 else 0
check("skill list", s == 200 and len(d["skills"]) >= 1)
s, d = call("GET", f"/api/skills/{sid}")
check("skill get", s == 200 and "问候语" in d.get("content", ""), d)

# 6. 评估（同会话两条，验证会话分组；输出应为 SKILL.md JSON，前端负责解析）
s, d = call("POST", "/api/eval", {
    "model": "mock-model-a", "persona_id": pid, "skill_id": sid,
    "input_text": "你好，请问在吗？"})
check("eval", s == 200 and "overall_issue" in d.get("output", "") and d.get("session_id"), d)
eid = d.get("id", "")
sid_1 = d.get("session_id", "")
s, d2 = call("POST", "/api/eval", {
    "model": "mock-model-a", "persona_id": pid, "skill_id": sid,
    "input_text": "第二条测试（同会话）", "session_id": sid_1})
check("eval same session", s == 200 and d2.get("session_id") == sid_1, d2)
eid2 = d2.get("id", "")

# 7. 反馈（correct/incorrect）
s, d = call("POST", f"/api/eval/{eid}/feedback", {"feedback": "correct"})
check("feedback correct", s == 200 and d.get("feedback") == "correct", d)
s, d = call("POST", f"/api/eval/{eid}/feedback", {"feedback": "incorrect"})
check("feedback repeat blocked", s == 200 and d.get("feedback") == "correct", d)
s, d = call("POST", f"/api/eval/{eid2}/feedback", {"feedback": "incorrect"})
check("feedback incorrect", s == 200 and d.get("feedback") == "incorrect", d)

# 8. 会话 + 历史 + 统计
s, d = call("GET", "/api/sessions")
check("sessions list", s == 200 and len(d["sessions"]) >= 1, d)
sess = d["sessions"][0]
check("session summary", sess["total"] == 2 and sess["correct"] == 1 and sess["incorrect"] == 1, sess)
s, d = call("GET", f"/api/sessions/{sess['session_id']}/evals")
check("session evals", s == 200 and len(d["evals"]) == 2, d)
s, d = call("GET", "/api/evals")
check("evals list", s == 200 and len(d["evals"]) >= 2, d)
s, d = call("GET", "/api/evals/stats")
check("evals stats", s == 200 and d["total"] >= 2 and d["correct"] >= 1 and d["incorrect"] >= 1, d)

# 9. 直接人设文本评估（不选人设下拉）
s, d = call("POST", "/api/eval", {
    "model": "mock-model-a", "persona_text": "临时人设内容", "skill_id": sid,
    "input_text": "临时测试"})
check("eval with raw persona", s == 200, d)

# 10. 默认配置锁定：存量数据迁移后 is_default=1，不可改删
s, d = call("GET", "/api/personas")
default_persona = next((p for p in d["personas"] if p.get("is_default")), None)
if default_persona:
    s1, _ = call("PUT", f"/api/personas/{default_persona['id']}", {"name": "x", "content": "y"})
    s2, _ = call("DELETE", f"/api/personas/{default_persona['id']}")
    check("default persona locked", s1 == 403 and s2 == 403, (s1, s2))
else:
    check("default persona locked", True, "no default persona in this db")
s, d = call("GET", "/api/skills")
default_skill = next((k for k in d["skills"] if k.get("is_default")), None)
if default_skill:
    s1, _ = call("DELETE", f"/api/skills/{default_skill['id']}")
    check("default skill locked", s1 == 403, s1)
else:
    check("default skill locked", True, "no default skill in this db")

# 11. 导出 CSV
import urllib.request as _ur
with _ur.urlopen(BASE + "/api/evals/export", timeout=30) as r:
    body = r.read().decode("utf-8").lstrip("﻿")
    check("export csv", r.status == 200 and "人工标注" in body and "正确" in body, body[:120].encode("gbk", "replace").decode("gbk"))

print()
print("TOTAL FAILURES:", len(fails), fails if fails else "")
raise SystemExit(1 if fails else 0)
