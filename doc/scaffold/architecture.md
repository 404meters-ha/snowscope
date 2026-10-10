# 架构文档 · 脚手架生成功能（scaffold）

> 上游：`PRD.md`（2026-09-22 定稿）｜下游：`stories.md`
> 原则：全部复用现有模式——JSON 列 + String 状态 + UTC、TaskManager 后台任务、行业洞察式两段交互、`llm.py` 集中 prompt、`_migrate` 加列。

## 1. 架构总览

```
App.vue 新 tab「🏗 脚手架」
   │  POST /api/scaffold/requests {text}          （一句话 → 后台任务 scaffold_match）
   ▼
scaffold_requests 表（状态机 + 全链路产出 JSON 列）
   │
   ├─ FR-1/2 匹配编排  app/services/scaffold.py
   │     本地 repos 库（tags/精析优先） + GitHubClient.search_repos
   │     双轨评分：scoring.py 新增 scaffold_fit() ｜ LLM：llm.py 新增 SCAFFOLD_* prompt
   │
   ├─ FR-3 拆条（同步 LLM，仿 industries/parse）
   ├─ FR-4 条目选型（后台任务，每条目 = 小型 industry_pipeline）
   │
   └─ FR-5/6 生成    app/services/scaffold_builder.py
         agent_sdk（WriteFile 白名单扩到 scaffolds/）→ 平台校验 → zipfile → /scaffolds 静态出 URL
         scaffold_caches 表：拆解/适配度/产物 三类指纹缓存
```

## 2. 数据模型（models.py 新增，db.py `_migrate` 无需加列——全新表）

### `scaffold_requests`（主表，一条 = 一次全链路）

| 列 | 类型 | 说明 |
|---|---|---|
| id | int PK | |
| user_id | int 默认 1 | 惯例预留 |
| raw_text | Text | 原始一句话 |
| need_brief | JSON | LLM 需求理解摘要 `{summary, features[], scale, constraints[]}`（match 任务产出，结果面板展示） |
| status | String(16) | `matching → matched → done_adopt`｜`splitting → split → selecting → selected → building → built`（失败不换状态，靠任务 failed + 重试） |
| tech_stack | String(64) | 主技术栈推断（条目确认时可改，如 `Python`） |
| adopt_repo | String(128) | 采用分支：仓库 full_name |
| framework_candidates | JSON | 整体匹配候选 `[{full_name, stars, language, desc, clone_url, fit_score, rule_score, llm_score, reason, in_lib}]` |
| items | JSON | 技术条目 `[{no, name, desc, keywords[], candidates[], selected(null=自研|full_name), self_dev}]`，candidates 结构同上 |
| adopt_report_md | Text | 采用分支的评估报告 |
| build | JSON | 生成结果 `{fingerprint, zip_key, zip_url, file_count, total_bytes, licenses[], warnings[]}` |
| created_at / updated_at | DateTime UTC | |

### `scaffold_caches`（指纹缓存，FR-6 的 2/3/4 层）

| 列 | 类型 | 说明 |
|---|---|---|
| key | String(64) PK | sha1 指纹 |
| kind | String(16) | `decompose` / `fit` / `build` |
| payload | JSON | 缓存体（条目清单 / 适配度结果 / {zip_key, build} 清单） |
| created_at | DateTime | |

指纹口径：
- **拆解缓存**：`decompose:{sha1(规范化 raw_text)}` → items 骨架（不含候选）
- **适配度缓存**：`fit:{sha1(条目 name+desc+keywords 规范化)}:{repo_full_name}` → 评分结果（整体匹配阶段条目指纹 = 需求指纹）
- **产物缓存**：`build:{sha1(items 终态 + selections + "v1")}` → zip_key（版本号入指纹，生成规范升级后自动失效）

### 产物落盘

- 工作区：`scaffolds/workspace/{request_id}/`（Agent 写盘区，gitignore）
- zip 产物：`scaffolds/{request_id}/scaffold.zip`，`main.py` 静态挂载 `/scaffolds`（与 `/courses` 同待遇，html=True 不需要；zip 直链下载）

## 3. API 设计（app/api.py 新增 8 个端点）

| 端点 | 方法 | 说明 | 同步/任务 |
|---|---|---|---|
| `/api/scaffold/requests` | POST | `{text}` 创建 + 提交匹配 | 任务 `scaffold_match` |
| `/api/scaffold/requests` | GET | 分页列表（`limit/offset/total`，排序 id 倒序） | 同步 |
| `/api/scaffold/requests/{id}` | GET | 详情（全字段） | 同步 |
| `/api/scaffold/requests/{id}/match` | POST | 改话重跑匹配（回退用） | 任务 `scaffold_match` |
| `/api/scaffold/requests/{id}/adopt` | POST | `{full_name}` 采用：置 done_adopt + 同步生成评估报告（单次 LLM 调用，参照 industries/parse 的同步先例） | 同步 |
| `/api/scaffold/requests/{id}/split` | POST | 拆条（查拆解缓存）→ status=split | 同步（单次 LLM，2-10s） |
| `/api/scaffold/requests/{id}/items` | PUT | `{items, tech_stack}` 条目确认回写 → 提交选型 | 任务 `scaffold_select` |
| `/api/scaffold/requests/{id}/select` | POST | `{selections:[{no, full_name\|null}]}` → 提交生成 | 任务 `scaffold_build` |

约定：`select`/`items`/`match` 可重复调用 = 重试/回退重跑的自然实现；`GET /api/tasks` 轮询复用。

## 4. 服务层设计

### `app/services/scaffold.py`（匹配/拆条/选型编排）

- `match_pipeline(request_id, ...)`：LLM 归纳 need_brief + 检索关键词规划（仿 `plan_industry`）→ 本地库筛选（需求关键词 × tags/desc LIKE）+ `search_repos`（有 token 5 组关键词 / 无 token 减半，限流即止）→ 去重取 5-8 → 逐个双轨评分（fit 缓存优先）→ 回写 `framework_candidates` + status=matched
- `split_items(request_id)`：拆解缓存查 → miss 则 LLM（`SCAFFOLD_SPLIT_SYSTEM`）出 3-8 条 + tech_stack 推断 → 写缓存 + status=split
- `select_pipeline(request_id)`：每条目串行：关键词 → tags 归一（`ensure_tags`）→ 库内 + search_repos → 候选 3-5 → 双轨评分（fit 缓存）→ 回写 items[].candidates → status=selected
- 入库副作用：候选中的新仓库 `_upsert_repo` + 打标签（与 industry_pipeline 一致，喂情报站主库）

### 双轨评分 `scoring.py::scaffold_fit(repo, need_text)`

- 规则分 0-40：语言命中用户技能画像（+15）、topics/desc 技术关键词命中（每词 +5 封顶 15）、活跃度（push 新鲜度 + star 档位，10）
- LLM 分 0-60：`SCAFFOLD_FIT_SYSTEM`（需求/条目文本 vs 项目定位+README 前 2KB，输出 `{score, reason}`）
- 合成 `fit_score = rule + llm`，落候选结构 `{fit_score, rule_score, llm_score, reason}`

### `app/services/scaffold_builder.py`（FR-5 生成管线）

1. 计算组合指纹 → `scaffold_caches` 命中则直接回写 build（秒回，不重生成）
2. Agent 运行（`agent_sdk.run`）：system prompt 注入 = 需求摘要 + 条目/选型/理由 + base 框架建议（由 `SCAFFOLD_BASE_SYSTEM` 单次 LLM 预判）+ **生成规范**（六件套清单、验收线、结构化 TODO 格式、依赖分层规则）+ 工作区路径
3. 平台侧校验：六件套文件存在性 + 顶层 README 启动说明存在（缺失则任务 failed，日志指明缺件）
4. `zipfile` 打包工作区 → `scaffolds/{request_id}/scaffold.zip` → 登记产物缓存 → 清理 workspace → 回写 build + status=built

### prompt 清单（llm.py 新增 4 个 SYSTEM）

`SCAFFOLD_BRIEF_SYSTEM`（需求归纳）/ `SCAFFOLD_SPLIT_SYSTEM`（拆条 JSON）/ `SCAFFOLD_FIT_SYSTEM`（适配度评分）/ `SCAFFOLD_BASE_SYSTEM`（base 框架预判）；生成规范正文放 `scaffold_builder.py`（长文本，不走 llm.py 惯例的位置也可，标注）。

## 5. Agent 边界调整

- `agent_sdk.py` WriteFile 白名单：`courses/` → 增 `scaffolds/workspace/**`（`_resolve_under` 模式不变，16 项边界测试补充新路径用例）
- `MAX_TURNS`：build 任务专用调大到 120（/tech 课程 16 轮起步，整项目 10+ 文件预计 40-80 轮）
- 超时沿用 `skill_run_timeout`；build 任务进度走事件流（现有节流上报）

## 6. 前端设计（App.vue + api.js）

- 新 el-tab「🏗 脚手架」（repos/issues/industries/learning/skills 之后）：
  - 顶部：一句话输入框 + 「开始匹配」按钮（空文案禁用）
  - 需求卡片列表：状态 tag（匹配中/已匹配/已采用/拆条/选型/生成中/已生成）+ 一句话原文 + 时间 + 「继续/查看」
  - 详情抽屉（复用行业报告抽屉模式），按 status 分区渲染：
    - matched：need_brief 展示 + 候选表格（适配度进度条、理由、「采用」按钮、「拆条继续」按钮）
    - split：条目编辑面板（el-input 行可增删改 + tech_stack 下拉）→「确认并选型」
    - selected：逐条目候选勾选（适配度排序，支持「自研」）→「生成脚手架」
    - built：六件套清单 + zip 下载链接 + 重生成按钮
    - done_adopt：clone URL 复制 + 评估报告（mdToHtml 渲染）
- `focusTask` 跟踪四类任务，完成后刷新详情（现有模式零改动）
- api.js 新增 8 方法与端点一一对应

## 7. 状态机与回退

```
matching ──✓── matched ──adopt── done_adopt（终态）
              │ ▲match 重跑
              └split──split ──PUT items── selecting ──✓── selected
                        ▲（重复 PUT = 改条目重选型）              │select 重跑
                                                            building ──✓── built
```

失败：任务 failed 不改 status，卡片显示「重试」= 重复调对应端点。

## 8. 分期映射

| 期 | 后端 | 前端 | 备注 |
|---|---|---|---|
| V1 | 表 + match/adopt 端点 + scaffold.py 匹配编排 + scaffold_fit | tab + 输入 + 卡片列表 + matched 详情抽屉（采用/报告） | 评分口径与缓存表可先行（fit 层） |
| V2 | split/items/select 端点 + 拆解缓存 + 选型编排 | 条目编辑面板 + 选型面板 | |
| V3 | scaffold_builder + 产物缓存 + WriteFile 白名单扩展 + /scaffolds 挂载 | 生成/下载面板 | **动工前先跑技术验证门** |

## 9. 技术验证门（V3 前置，独立小任务）

用 agent_sdk 按生成规范跑一次示例需求（「待办事项 Web 应用」，选型 FastAPI+Vue），人工检查：六件套齐全、`pip install && 启动命令` 可跑、页面可开、TODO 结构化。不过关 → 回用户重谈生成方案（候选：拆多阶段生成 / 收窄验收线）。
