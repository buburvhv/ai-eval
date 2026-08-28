# AI 辅助评估工具（ai-eval）

模型自动评估 Web 工具：配置模型网关后，大模型结合「用户人设」与「Skill 评估标准」对输入信息进行自动评估，输出结构化评估报告（六维评分、主要问题、改进建议），并支持人工点赞/点踩标注，数据存入本地 SQLite。

> 适用于百度研发场景：可部署到内网 / POPO，多用户通过浏览器使用，作者预置默认模型网关、人设、Skill 配置，对其他用户只读。

## 功能特性

- **基于 Skill 决策的自动评估**：上传 .md/.txt 评估标准文件（SKILL.md），选择人设与模型后自动逐轮评估，输出 JSON 结构化报告。
- **会话历史**：左侧按会话分组展示历史；**发送即创建会话**（后端先落库占位，模型返回后更新结果），刷新页面不丢当前会话，同一会话内可连续追加评估。
- **人工标注**：每条结论可标注「正确 / 不正确」，实时写入服务端 SQLite，支持一键导出 CSV（带 BOM，Excel 打开不乱码）。
- **模型网关探测**：通用 OpenAI 兼容接口，自动探测常用端口，兼容厂内网关、vLLM、Ollama 等。
- **多用户隔离**：默认配置（作者预置）对所有用户只读；用户自定义模型配置仅存本浏览器 localStorage。

## 启动

```bash
cd ai-eval
pip install -r requirements.txt
python server.py
```

打开浏览器访问 http://127.0.0.1:8790

> 监听 `0.0.0.0:8790`：本机开发用 127.0.0.1 访问，部署到内网 / POPO 时其他机器可通过服务器 IP:8790 访问。

## 部署（GitHub 模板方式）

仓库自带一个 **模板 `data.db`**（不含密钥、不含评估历史）：

- **模型配置**：默认 Base URL = `https://oneapi-comate.baidu-int.com:443/v1`，API Key 已置空
- **人设**：预置「太虚阁·Amber·衣橱主理人」作为默认人设
- **Skill**：预置「数字人评估skill-0828」作为默认评估标准
- **评估历史**：空表

克隆后：

```bash
git clone <仓库地址> && cd ai-eval
pip install -r requirements.txt
python server.py
```

首次使用，在**设置页 → 模型配置**点击「使用我的配置」，填上自己的 API Key（或删除默认 URL 换成你自己的网关链接），保存后即可评估。默认配置展示但只读，改用自己的配置仅保存在本浏览器，不影响其他用户。

服务监听 `0.0.0.0:8790`，内网机器访问 `http://<服务器IP>:8790` 即可，把地址发给同事即可用。

## 安全说明

- 仓库中的 `data.db` 为**模板**：`model_config.api_key` 已置空，不包含真实密钥与评估数据。
- 本地开发产生的真实 `data.db` 请勿提交（gitignore 已排除 `data.db.bak` 等备份）。
- 部署机若需在目标库写入默认网关配置（未用模板、空库场景），可用：

  ```bash
  curl -X POST http://<目标机IP>:8790/api/seed-default-config \
    -H "Content-Type: application/json" \
    -d '{"base_url": "http://model-gateway.xxx.com", "api_key": "sk-xxx", "port": null}'
  ```

## 使用流程

1. **模型配置**：设置页填写网关 Base URL 与 API Key（可选端口），保存时自动探测 `/models` 接口，下拉选择模型后可用「测试调用」验证连通性。
   - 支持 `http://host`、`http://host:port` 两种写法；不填端口时自动探测常用端口。
   - 兼容 OpenAI 接口的网关均可接入（厂内网关、vLLM、Ollama 等）。
2. **用户人设**：填写人设名称 + 内容保存，之后从主页面下拉直接载入复用。
3. **评估标准（Skill）**：上传 skill 文件（.md/.txt），自定义命名保存，主页面下拉选择。
4. **评估**：选择 Skill（必选）、模型（必选）、人设（可选），粘贴待评估内容，点击发送。评估中发送按钮变为「■ 停止」，可随时中止；超过 5 分钟会提示输出较慢。
5. **反馈**：对评估结果标注「正确 / 不正确」，标注自动保存到 SQLite，可导出 CSV。

## 数据存储

- `data.db`：SQLite 数据库（模型配置、人设、Skill、评估记录及反馈）。可用环境变量 `AI_EVAL_DB` 指定路径。
- `skills/`：上传的 Skill 源文件。

## 接口摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/seed-default-config | 写入/更新默认模型配置（部署初始化） |
| POST | /api/model/test | 探测网关并返回模型列表（不落库） |
| GET | /api/model/config | 获取默认（作者内置）模型配置 |
| POST | /api/model/models | 按请求配置（或默认配置）获取模型列表 |
| POST | /api/model/test-chat | 测试所选模型调用 |
| GET/POST | /api/personas | 人设列表 / 保存人设 |
| PUT/DELETE | /api/personas/{id} | 更新 / 删除人设（默认配置返回 403） |
| GET/POST | /api/skills | Skill 列表 / 上传 Skill |
| GET/DELETE | /api/skills/{id} | 预览 / 删除 Skill（默认配置返回 403） |
| POST | /api/eval | 调用模型评估（先落库占位，模型返回后更新） |
| POST | /api/eval/{id}/feedback | 点赞/点踩标注 |
| GET | /api/sessions | 会话分组历史 |
| GET | /api/sessions/{sid}/evals | 某会话的全部评估 |
| GET | /api/evals, /api/evals/stats | 历史记录 / 统计 |
| GET | /api/evals/export | 导出评估记录 + 标注 CSV |

## 测试

```bash
python test_smoke.py
```

起一个 mock 模型网关（127.0.0.1:18081）验证全部 API 流程（模型探测、人设/Skill CRUD、评估、反馈、会话、导出）。

## 前端说明

- 主界面：上下文栏（Skill / 人设 / 模型）+ 消息流（单列评估报告卡）+ 发送区（评估中变为停止按钮）。
- 设置页：模型配置（默认锁定只读）、人设管理、Skill 管理、数据导出。
- 评估中提示：初始「模型正在按 Skill 标准逐轮评估，请稍候…」，超过 300 秒追加「评估已超过 5 分钟，输出较慢」。
