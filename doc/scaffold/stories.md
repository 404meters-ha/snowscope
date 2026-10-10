# 用户故事清单 · 脚手架生成功能（scaffold）

> 上游：`PRD.md` + `architecture.md`（2026-09-22）
> 开发顺序即列表顺序；每个故事完成即独立可验收，`[ ]` 勾选表示已交付。
> AC = 验收标准（Acceptance Criteria）。

## V1 · 整体匹配闭环

### S1 匹配管线（后端）`[x]`（2026-09-22 E2E 通过）

作为用户，我提交一句话需求，平台后台检索出 5-8 个适配的开源框架并给出适配度百分比与理由。

范围：`models.py`（scaffold_requests + scaffold_caches 表）、`services/scaffold.py`（match_pipeline）、`scoring.py::scaffold_fit`、`llm.py`（SCAFFOLD_BRIEF_SYSTEM）、`api.py`（POST/GET requests 三端点）、`.gitignore` 补 `scaffolds/`。

- [x] AC1: `POST /api/scaffold/requests {"text":"..."}` 创建记录（status=matching）并返回任务 id；`GET /api/scaffold/requests` 分页 `{requests, total}`；`GET /{id}` 返回详情
- [x] AC2: 任务成功后详情含 `need_brief`（LLM 需求摘要）与 `framework_candidates` 5-8 个，每个含 `fit_score/rule_score/llm_score/reason/clone_url`，status=matched
- [x] AC3: 候选含本地库命中项（`in_lib=true`）与 GitHub Search 补充项；限流时降级不崩（任务仍成功，候选数可少）
- [x] AC4: 同一需求重复匹配命中 `scaffold_caches` 的 fit 缓存（第二次运行 LLM 评分调用数为 0）——E2E 实测 `fit_cached: 8`
- [x] AC5: 候选中的新仓库已 `_upsert_repo` 入库并打标签（domain 归一 tag，实测 miniflux/v2 等已带「RSS 聚合阅读站」）

> E2E 实测（8101 测试实例）：「RSS 聚合阅读站」→ 候选 8 个，top3 = miniflux/v2(78.0) /
> FreshRSS(73.6) / Folo(68.5)，双轨分与理由正常；附带修复 `scoring._days_since` 对 SQLite
> naive datetime 的时区补齐（本地库路径曾崩）。

依赖：无。

### S2 前端 tab 与匹配面板 `[x]`（2026-09-22 Playwright E2E 通过）

作为用户，我在新 tab 输入一句话、看到任务进度、在详情里浏览候选与适配度。

范围：`App.vue` 新 tab + 详情抽屉 matched 态、`api.js` 4 方法（含 rematch）、focusTask 跟踪、
后端补 `POST /{id}/match` 重跑端点。

- [x] AC1: 新 tab「🏗 脚手架」：输入框（空禁用）+ 提交 → 卡片列表出现新记录，任务面板显示进度，完成后卡片状态变「已匹配」
- [x] AC2: matched 详情抽屉：需求摘要展示 + 候选表格（适配度进度条 + 分数 tooltip 显示规则分/LLM 分 + 理由列）
- [x] AC3: 页面刷新后卡片列表与状态正确恢复（3s 通用轮询接手 running 任务）
- [x] AC4: 「重新匹配」可改话重跑（回退）

> 锚点需求全链路实测：「流放之路洗装备工具」→ domain「游戏辅助工具（流放之路装备洗练）」，
> top3 = pyoe2-craftpath / PoETheoryCraft / lazy_crafter（全是真实 PoE 洗装项目，边缘项正确降分）。
> 附带修复：任务面板标识「🏗 需求 #n」改为创建即挂 request_id（原要等管线收尾才可见）。

### S3 采用分支 `[x]`（2026-09-22 E2E 通过，V1 完结）

作为用户，我对最适配的框架点「采用」，拿到 clone 地址和评估报告。

范围：`api.py` adopt 端点（同步 LLM 报告）、`llm.py` 评估报告 prompt、前端采用按钮 + clone 复制 + 报告渲染（mdToHtml）。

- [x] AC1: `POST /{id}/adopt {"full_name"}` → status=done_adopt，详情含 `adopt_repo` 与 `adopt_report_md`（中文 markdown：定位/适配理由/上手要点/风险）
- [x] AC2: 前端 done_adopt 态：clone URL 一键复制 + 报告抽屉渲染
- [x] AC3: 已 done_adopt 的请求重复 adopt 幂等（返回既有结果，不重复调 LLM）

> E2E：采用 miniflux/v2 → 报告 904 字（「推荐采用。Miniflux 覆盖 RSS 阅读站的核心需求…」），
> clone 输入框 + 复制按钮 + 列表「已采用 | miniflux/v2 | 查看报告」；幂等秒回（0.02s）。
> 已知边界：两个 adopt 并发撞在 14s 生成窗口内会各生成一次（前端 loading 已挡双击，自用可接受）。

## V2 · 拆条与条目级选型

### S4 技术条目拆解与确认 `[x]`（2026-09-22 E2E 通过）

作为用户，框架都不合适时我点「拆条继续」，系统拆出技术条目，我可以增删改后确认。

范围：`api.py` split 端点（同步 LLM）、`llm.py` SCAFFOLD_SPLIT_SYSTEM、拆解缓存、前端条目编辑面板。

- [x] AC1: matched 态点「拆条」→ 2-10 秒返回 3-8 条条目（编号/名称/职责/关键词）+ 主技术栈推断，status=split
- [x] AC2: 条目面板可增/删/改名称、描述、关键词；tech_stack 可改（下拉：Python/Java/JavaScript/Go/其他）
- [x] AC3: 「确认并选型」提交 PUT items → status=selecting，任务面板跟踪
- [x] AC4: 相同一句话的需求再次拆条命中拆解缓存（LLM 调用数为 0，面板秒出）

> E2E：「流放之路洗装备」拆出 6 条（Web 框架/词缀数据库/概率模拟/成本估算/数据存储/
> 游戏数据采集），tech_stack=TypeScript；「截图比对」拆出 7 条 tech_stack=Python；
> 编辑（改名+加条）后确认选型到 selected；缓存秒回 0.08s。

### S5 条目级选型 `[x]`（2026-09-22 E2E 通过，V2 完结）

作为用户，我逐条查看每个技术条目的候选开源项目并勾选（或标自研）。

范围：`scaffold.py::select_pipeline`（每条目串行：关键词→tags 归一→库内+Search→候选 3-5→双轨评分）、前端选型面板。

- [x] AC1: 选型任务成功后每条目含 candidates 3-5 个（fit 排序，含 reason），status=selected
- [x] AC2: 选型面板逐条勾选，支持「自研」（不选任何候选）；全部条目有归属后「生成脚手架」按钮可用（本期为占位，点击提示等待 V3）
- [x] AC3: 重新编辑条目（重复 PUT items）会重跑选型且已有 fit 缓存的候选不重复调 LLM——实测原样重交 `fit_cached: 30`（30 候选全命中，LLM 零调用）
- [x] AC4: 选型过程中 tag 归一走 `ensure_tags`（新词入 tags 表）——条目名入 tags 表（source=scaffold），候选入库打条目标签（RePoE→「词缀数据库」）

依赖：S4。

## V3 · Agent 生成脚手架

### S6 技术验证门 `[x]`（2026-09-22 通过，见 validation.md）

作为开发者，我在写生成管线前先验证 agent_sdk 能生成满足验收线的项目。

范围：`agent_sdk.py` WriteFile 白名单扩展（`scaffolds/workspace/**` + 边界测试用例）、MAX_TURNS build 档 120、生成规范草稿（scaffold_builder 内常量）、dry-run 脚本/操作记录。

- [x] AC1: 示例需求「待办事项 Web 应用」（选型 FastAPI+Vue）跑 agent 生成，产出含六件套
- [x] AC2: 人工核验：装依赖 → 一条命令启动 → 页面可开、TODO 结构化
- [x] AC3: 结论写入 `doc/scaffold/validation.md`；不过关则触发方案重谈（不进入 S7）
- [x] AC4: WriteFile 边界测试新增 scaffolds 路径用例全过（含穿越攻击用例）

> 附带修复：cc-mini openai 路径对第三方端点加 thinking disabled（首轮曾因 reasoning 挤占
> max_tokens 全截断）；`agent_sdk.run` 透传 max_tokens（生成任务 16384）。

依赖：无（建议与 V1/V2 并行或提前）。

### S7 生成管线 `[x]`（2026-09-22 E2E 通过）

作为用户，我选完型后点「生成脚手架」，后台 Agent 产出可启动的 zip 供下载。

范围：`scaffold_builder.py`（base 预判 SCAFFOLD_BASE_SYSTEM → agent_sdk.run → 六件套校验 → zipfile → 产物缓存 → build 回写）、`main.py` 挂载 `/scaffolds`、`api.py` select 端点、前端生成/下载/重生成面板。

- [x] AC1: select 提交 → scaffold_build 任务 → 成功后 status=built，详情含 zip_url 可下载
- [x] AC2: zip 六件套齐全；解压后按 README 三步内启动成功（页面可开 = 验收线）
- [x] AC3: 结构化 TODO 存在且标注对应条目与参考项目
- [x] AC4: 选型（依赖/vendor 分层）正确落位：包型进依赖清单、应用型进 vendor/ 或 clone 脚本
- [x] AC5: 同组合重新生成命中产物缓存秒回（Agent 不重跑）；改任一条目或选型后指纹变化、真重跑
- [x] AC6: 生成失败（如缺件）任务 failed、日志指明缺什么、可重试
- [x] AC7: build 进度事件实时进任务面板时间线

> E2E 实测（请求 #6「流放之路洗装备工具」，6 条目全选开源）：12 轮 / $0.38 / 25 文件 / 18KB，
> base=vercel/next.js（预判理由与挂载关系回填 build）。启动验收：解压 → npm install →
> npm run dev → 首页 200（5s），/api/mods 出真实词缀池、/api/craft 出千次模拟+成本+方案对比；
> TODO 11 处格式统一；nocodb（应用型）落 vendor/clone.sh、包型进 requirements.txt；
> 缓存重生成秒回（cache_hit=true，0 轮 0 成本），指纹单验改选型/技术栈即变。
> **E2E 暴露的两类生成质量问题已回写规范**（SCAFFOLD_SYSTEM 质量要求 + 指纹升 v2）：
> ① tsconfig.json 未配 typescript devDep（触发 Next 启动时自动安装，网络受限即挂起）；
> ② app/api/**/route.js 引根目录 lib/ 少退一级 import（运行时 500）——修正后 API 全 200。
> 附带修复：六件套校验的「前端页面」从只认 index.html 扩为 FRONTEND_GLOBS
> （首跑生成 Next.js app/page.jsx 被误判缺件失败，日志明确指出、重试即过的路径已实证）。
> v2 指纹同时透传 research.md 全链素材（整体匹配实录 + 每条目候选对比），S8 全链 E2E 验收。

依赖：S5 + S6。

### S8 收尾与复用验收 `[x]`（2026-09-22 E2E 通过，见 validation.md）

作为用户，整个链路的决策过程在 zip 的调研文档里完整留痕，平台文档同步。

范围：调研文档生成规范完善（全链路汇编进 docs/research.md）、CLAUDE.md API 段更新、全链路 E2E、memory 更新。

- [x] AC1: zip 的 docs/research.md 含：原始需求 → 整体匹配对比 → 每条目候选对比与选型理由 → 参考链接
- [x] AC2: CLAUDE.md 增补 scaffold API 速览；项目 memory 更新为已上线
- [x] AC3: 全链 E2E：一句话 → 拆条 → 选型（含一条自研）→ 生成 → 下载启动成功
- [x] AC4: 缓存复验：同需求第二次全链，LLM 调用与 Agent 运行次数显著下降（拆解/fit/产物三层缓存命中）

> E2E 实测（需求「个人知识库问答工具」，请求 #9/#10，7 条目、第 6 条「RAG 编排」自研）：
> 第一遍全链 14 轮 / $0.65 / 25 文件，research.md 四段全（含自研理由与 20 条参考链接）；
> 第二遍同需求：拆解缓存命中（0 LLM）、fit 35/35 全命中（0 LLM）、产物缓存 1.1s 秒回（0 Agent）。
> E2E 暴露的问题回写：① 子包相对导入少一个点（`from .config` 应为 `..config`）两轮均中招——
> 平台新增 `_check_imports` 静态校验门 + 规范加硬 + 指纹升 v3（带病缓存失效），重生成后导入零问题；
> ② Milvus Lite 无 Windows 轮子（选型未考虑目标机 OS，pip 静默跳过 extra、启动即挂）——
> 规范加跨平台提示（SQLite 系或 README「已知限制」），不升指纹（启动验收线本就通过）。

依赖：S7。

---

## 故事依赖图

```
S1 ─ S2 ─ S3（V1 完）
      ├─ S4 ─ S5 ──────┐
S6（并行/提前）─────────┴─ S7 ─ S8（V3 完）
```
