# S6 技术验证门结论 · Agent 脚手架生成能力

> 日期：2026-09-22｜结论：**✅ 通过，V3（S7）可按 architecture.md 方案开工**

## 验证内容

示例需求「待办事项 Web 应用」（选型 FastAPI + Vue 3 + SQLite 自研），用 `scripts/scaffold_dryrun.py`
走 `agent_sdk.run` + `SCAFFOLD_SYSTEM` 生成规范，产物落 `scaffolds/workspace/dryrun/`。

## 结果

| 项 | 结果 |
|---|---|
| 产物 | 13 个文件，六件套齐全（代码骨架/前端/调研/需求/架构/LICENSES） |
| 生成成本 | 4 轮 / $0.11 / 88s（远低于预估 10-30 分钟，thinking 关闭后） |
| 启动核验 | `uvicorn app.main:app` → 页面 HTTP 200（Vue 3 CDN 零构建）+ `/api/todos` 真实响应（sqlite 已初始化） |
| 结构化 TODO | 1 处，格式合规（标注条目名与参考） |
| 选型落位 | fastapi/uvicorn 进 requirements.txt（包型）；Vue 走 CDN；sqlite3 标准库自研 |

## 过程中暴露并修复的问题（两个真坑）

1. **cc-mini openai 路径未关思考**：glm-4.7 的 reasoning_content 挤占 max_tokens（回落 8192），
   首轮即 `finish_reason=max_tokens` 截断、零文件产出。修复：`vendor/cc-mini/src/core/llm.py`
   `_build_openai_request` 对第三方 base_url 加 `extra_body={"thinking": {"type": "disabled"}}`
   （平台自身 `app/services/llm.py` 早有同款修复，agent 链路此前没吃到）。
2. **8192 单轮额度不足**：`agent_sdk.run` 增加 `max_tokens` 透传，生成类任务传 16384（`BUILD_MAX_TOKENS`）。

## 附带基础设施（S6 范围）

- `agent_sdk` 写白名单多根化：`WRITE_ROOTS = [courses/, scaffolds/]`，读/列白名单同步扩 scaffolds/
- `run()` 新增 `system_prompt` / `max_tokens` 参数（任务型调用方自定义身份）
- `tests/test_agent_sdk_boundaries.py`：17 项边界断言全过（穿越/前缀伪造/越权/绝对路径）
- `app/services/scaffold_builder.py`：SCAFFOLD_SYSTEM 生成规范 + build_prompt 组装（S7 直接复用）

## 对 S7 的输入

- 生成质量与成本可行（$0.11/次），「改上游重生成」的迭代策略成本可接受
- 4 轮完成说明 MAX_TURNS 120 余量充足
- 待 S7 落地：六件套校验自动化、zipfile 打包、产物指纹缓存、`/scaffolds` 静态挂载

---

# S8 全链 E2E 与缓存复验 · V3 主线完结

> 日期：2026-09-22｜结论：**✅ 通过，脚手架 V1-V3 全量上线**

## 验证内容

需求「个人知识库问答工具：导入 Markdown 笔记做本地向量化，网页上提问并给出带引用的回答」
（请求 #9/#10，7 技术条目，第 6 条「RAG 编排」**自研**）：一句话 → 匹配 → 拆条 → 条目选型 →
逐条选型提交 → Agent 生成 → 下载解压 → 独立 venv 装依赖 → 启动核验；同需求第二遍全链对照缓存命中。

## 结果

| 项 | 第一遍（#9，真跑） | 第二遍（#10，同需求） |
|---|---|---|
| 匹配 | 8 候选（Langchain-Chatchat 81 分居首），fit 8 次全新评 | fit 5/8 命中缓存（3 个为搜索波动新候选） |
| 拆条 | 1 次 LLM，7 条目 + tech_stack=Python | **cached=True，0 LLM 秒回** |
| 条目选型 | 7 条目 × 5 候选，fit 35 次全新评 | **fit 35/35 全命中，0 LLM** |
| 生成 | 14 轮 / $0.65 / 25 文件（base=fastapi） | **产物缓存 1.1s 秒回，0 Agent 0 成本** |
| research.md | 四段全：需求归纳 / 整体匹配对比表+拆条理由 / 逐条目对比（含自研理由）/ 20 条参考链接 | 同产物复用 |
| 启动核验 | venv（Py3.13）装依赖 → uvicorn → 首页 200 + /docs 200 + 5 条路由注册 | — |

## 过程中暴露并修复的问题（两个真坑）

1. **子包相对导入少一个点**：`app/services/embedding.py` 与 `ingest.py` 写成 `from .config import`
   （config 在 `app/` 根，应为 `..config`），`uvicorn app.main:app` 启动即 ModuleNotFoundError——
   S7 规范里的「import 实存核对」只压住了 JS 侧，Python 侧两轮生成均中招。修复三件套：
   ① 平台侧新增 `_check_imports` 静态校验门（ast 解析相对导入，点数=上退级数，目标必须实存，
   命名空间包兼容），与六件套校验同点位拦截；② SCAFFOLD_SYSTEM 质量要求写明 Python 点级规则；
   ③ 组合指纹 v2→v3，带病产物缓存自动失效。对带病树实测：精准命中 2 个错文件、10 个正确导入零误报；
   v3 重生成（11 轮 / $0.52 / 20 文件，改用绝对导入）导入零问题、启动通过。
2. **Milvus Lite 无 Windows 轮子**：选型落了 milvus（Milvus Lite 本地文件模式），但 milvus-lite
   只支持 Ubuntu/macOS——`pip install pymilvus[milvus-lite]` 在 Windows 上**静默跳过**该 extra，
   深链路（导入笔记）首调即 `ConnectionConfigException`。属「选型未考虑目标机 OS」而非生成错误
   （启动验收线本就通过），处置：规范加跨平台提示（本地文件型组件优先 SQLite 系，或把限制写进
   README「已知限制」），不升指纹。

## 其他记录

- 装依赖实测：默认 PyPI 可用（清华镜像在本机不通）；torch 2.14 / sentence-transformers 6.0 /
  transformers 5.17 在 Python 3.13 全绿
- v3 产物 requirements 较 v2 少 `python-multipart` 与 `milvus-lite` extra（LLM 生成波动，功能未受影响）
- E2E 驱动脚本在 `.research-s8/`（gitignore），关键数字以本文件为准
- 深链路（导入笔记→向量化→检索）在本机双重受阻，均为环境/选型层而非生成错误：HF 直连超时
  （本机需代理，`HF_ENDPOINT=https://hf-mirror.com` 可绕）、milvus-lite 无 Windows 轮子（见真坑②）
