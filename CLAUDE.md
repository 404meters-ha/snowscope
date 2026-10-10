# celestial-snow 平台速览（给无头运行的 AI）

GitHub Trending 情报站：抓取热门项目 → 规则 + LLM 双重评分 → issue 匹配推荐 → 贡献报告与学习闭环。
后端 FastAPI 跑在 `localhost:8100`，SQLite（`celestial.db`），前端 Vue 构建产物由本服务托管。

用户画像（`.env` 可覆盖，默认见 `app/config.py`）：`USER_PROFILE`（背景方向）、`USER_SKILLS`（技能清单，逗号分隔）。
给项目/issue 做推荐或打分时，围绕这份画像评估「与用户的匹配度」。

## 关键 API（curl 可直接访问）

- `GET /api/repos?sort=total|rule|stars&q=<关键词>&analyzed=all|done|todo&period=weekly|monthly&week=latest|YYYY-Www&tag=<标签>&tag=<标签2>&limit=N&offset=N` — 项目榜
  （分页返回 `{repos, total}`；字段：`full_name` `stars` `language` `total_score` `rule_score` `tags` `analyzed` `latest_report`。
  `analyzed=done` ⇔ LLM 精析完成；`tag` 可重复多选，多值 AND 交集；`tag`/`period` 是 JSON 列 LIKE 过滤，模式须走 `api._json_like`——中文存成 `\uXXXX`，裸拼 LIKE 匹配不到；
  `week` 是入库批次（东八区 ISO 周聚合 `first_seen_at`，刷新榜单后分新旧），`latest`=最新批次周；行内带 `new_batch` 标记最新批次的项目）
- `GET /api/repos/weeks` — 入库批次清单（倒序：week/count/label「本周|上周」/range 日期段/latest），批次下拉选项源；**注册在 `/repos/{full_name:path}` 之前**，别挪后
- `GET /api/repos/{full_name}` — 项目详情，含 README 全文缓存 `readme_cached`、LLM 精析 `llm_scores`、`latest_report`（贡献报告 id）
- `GET /api/issues?sort=match|rule|latest&difficulty=低|中|高&deep_only=true&analyzed=all|done|todo&limit=N&offset=N` — issue 排行榜
  （分页返回 `{issues, total}`；`analyzed=done` ⇔ `match_score` 非空（LLM 精筛过）；「疑似已修复/已关闭」按物化列 `issues.fixed_hint` 在 SQL 沉底，
  行内 `state`/`closed_at`/`fixed_by_pr` 标记 GitHub 侧状态与已合并修复 PR）
- `POST /api/repos/analyze` `{"repo_ids": [...]}` — 批量精析选中项目（≤10 个，任务类型 `analyze`，复用刷新流水线的单仓精析）
- `POST /api/repos/translate` — 为 `repos.zh_desc`（中文一句话简介）缺失的项目批量生成（已是中文的直接回填，其余 LLM 翻译；任务类型 `translate`；精析完成时也会用 core_idea 兜底回填）
- `GET /api/tags` — 标签 → 项目数汇总（`repos.tags` JSON 列，行业分析与全量打标都写这里）
- `POST /api/tags/auto` — 全量自动分类：LLM 先提 8-15 个分类体系（强制优先复用 `tags` 表已有标签），再分批给所有项目打 1-3 个标签（任务类型 `tagging`）
- `POST /api/industries/parse` `{"text": "agent运行时 pi deer-flow"}` — 行业分析输入解析（同步 2-5 秒）：LLM 拆「方向 vs 项目名」，
  方向经标签库归一给 canonical，项目名 GitHub 定位出候选清单——前端确认面板的数据源
- `POST /api/industries` `{"directions": [{"raw": "agent运行时", "tag": "Agent运行时"}], "repos": ["bytedance/deer-flow"]}` — 确认后的多方向分析
  （任务类型 `industry`，方向串行跑；种子项目并入各方向候选与报告，项目名本身不打成标签）；报告看 `GET /api/industries`（列表）/ `GET /api/industries/{id}`（含 `overview_md` 与项目分类清单）；
  `DELETE /api/industries/{id}` 删一份报告（只删报告记录，`repos.tags` 是累积知识不动）
- `GET /api/learning-context/{issue_id}` — 一次取全：issue + 仓库元数据 + README + 贡献报告深读段（深度分析/生成课程用这个）
- `GET /api/learning-context/repo/{repo_id}` — `/tech-repo` 项目课取材：仓库元数据 + 精析（README 全文）+ 最新贡献报告 + 匹配度 top5 issue
- `GET /api/learning-context/book/{book_id}` — `/tech-book` 教材课取材：大纲（章 → 页范围 → 逐页文本文件清单）+ 逐页文本目录
- `POST /api/books`（multipart：`file`=PDF、`title` 可空）— 上传教材 → `book_extract` 后台任务：pypdf 逐页抽文本，扫描页
  （抽出 <60 字符）渲染 PNG（pypdfium2）送 `LLM_VISION_MODEL`（默认 glm-5.3-flash）视觉转录，再 LLM 归纳学习大纲；
  逐页文本落 `uploads/books/{id}/text/pNNNN.txt`（文件名即页码；uploads 在 agent READ_ROOTS 内）
- `GET /api/books`（列表）/ `GET /api/books/{id}`（含 outline）/ `DELETE /api/books/{id}`（删记录与文件，已生成课程保留）
- `POST /api/books/{id}/series` `{"chapter_nos": [1, 3]}` — 章节系列课：对选中章**逐章**跑 /tech-book（单章模式，参数 `{book_id} 第{N}章`），一章一门课，串行不问询（任务类型 `book_series`）
- `POST /api/courses` 已多来源化：`source_type` = `issue`|`repo`|`book`|`import` + 对应 `issue_id`/`repo_id`/`book_id`（缺省按入参推断；`replace` 覆盖重生成仅 issue 课支持，repo/book 课另起新课并存）
- `POST /api/courses/import`（multipart：`file`=zip 或单个 HTML 课件页、`title` 可空、`series` 可空）— 导入已生成好的教程（/tech 产物结构或其发布副本）：
  zip 解压进 `courses/{新id}/`；单个 HTML 直接成一门 1 课的课，平台生成目录首页（复用 /tech 模板结构，带学习进度组件）。
  lesson_id 取页内 quiz-spec 内嵌值（发布副本中文名文件也对得上）、页面写死的旧 `course_id` 由平台改写成新 id
  （quiz 进度才能对上）、剥 `CELESTIAL_PUBLISHED` 标记、缺 `assets/quiz.js`/`style.css` 补默认件；zip 中文文件名按 cp437→gbk 重解码；
  `series` 填了则归入该系列（`courses.series` 列，同系列课在前端聚拢、按导入顺序编「第N门」，选项随 `GET /api/courses` 的 `series_options` 下发）；
  课名与已有导入课重复时顶掉旧课（`api._purge_duplicate_imports` 删记录+目录+quiz，返回带 `replaced_course_ids`）
- `POST /api/courses/{course_id}/lessons/delete` `{"lesson_ids":[…]}` — 删除选中的课件小节（前端课程卡多选）：
  lessons 清单、`courses/{id}/` 里对应 HTML、quiz 存档一并清；删空（含一次全选）则整门课连目录一起删（返回 `course_deleted`）
- `POST /api/courses/{course_id}/publish` — 把 `courses/{id}/` 发布到服务器本地磁盘（`LOCAL_PUBLISH_DIR`，默认 `./published`，平台静态托管在 `/published`），返回 `entry_url` 等清单
  （平台负责分文件夹、用中文课标题重命名课件、改写页面内相对链接；`{"prune": true}` 清掉上一版残留文件；nginx 接管时改 `LOCAL_PUBLISH_BASE_URL`）
- `GET /api/tasks?limit=N` — 后台任务状态；`GET /api/config` — 配置状态
- `POST /api/tasks/{id}/answer` `{"id":"<问题id>","answers":[{"question":"…","answer":"…"}]}` 或 `{"id":"…","cancel":true}` — 投递 waiting 任务的应答/取消（waiting 态有效；404 任务不存在、409×6 非 waiting/无桥/无等待问题/id 不匹配/问题已轮替/已应答已过期、400 形状不对；投递只留痕 `✅ 应答：`/`⛔`，状态翻转由工具侧清场）
- `POST /api/scaffold/requests {"text":"一句话需求"}` — 脚手架匹配任务（status=matching→matched，产出 `need_brief` 与 `framework_candidates` 5-8 个，
  双轨适配度 fit=规则0-40+LLM0-60）；`GET /api/scaffold/requests` 分页列表 / `GET /{id}` 详情；`POST /{id}/match` 改话重跑
- `POST /api/scaffold/requests/{id}/adopt {"full_name"}` — 采用分支（同步评估报告，终态 done_adopt）
- `POST /api/scaffold/requests/{id}/split` — 拆技术条目（同步 2-10 秒，matched→split；同需求命中拆解缓存秒回）；
  `PUT /{id}/items {"items":[…],"tech_stack":"…"}` — 条目增删改确认 → selecting 任务 → selected（每条目 candidates 3-5 双轨评分，条目关键词入 tags）
- `POST /api/scaffold/requests/{id}/select {"selections":[{no, full_name|null}]}` — 逐条选型提交（null=自研）→ scaffold_build 任务：
  base 预判 → Agent 生成六件套（README/前端页/docs 三件/LICENSES）→ 校验打包 → status=built，`build` JSON 含 zip_url/base 主干/license 告警；
  产物 `GET /scaffolds/{id}/scaffold.zip` 直链下载（main.py 静态挂载，工作区 scaffolds/workspace/ 用完即清）
- 三层缓存（`scaffold_caches` 表，内容寻址指纹）：拆解（需求→条目）/ fit（条目+项目→分）/ build（组合指纹→产物，同组合重生成秒回不重跑 Agent；
  生成规范升级靠指纹版本号升版自动失效（当前 v3：相对导入静态校验门））

## 约定

- 结论要能支撑决策：值不值得投入这个项目 / 这个 issue 适不适合用户上手，给出理由与下一步动作。
- 输出用精炼的结构化 Markdown。
- 生成课程走技能家族，不要手工绕过流程：`/tech`（issue 课）、`/tech-repo`（项目课：导览/架构走读/上手路径）、`/tech-book`（教材课：整本精讲或「第N章」单章模式）。
  三者模板与 assets 全部复用 `.claude/skills/tech/`，新课程技能不要复制模板。
- 课程四种来源（`courses.source_type`）：issue / repo / book / import——列表视图与前端卡片按它分支渲染来源行；发布文件夹名也按它分支。
- 课程有本地与发布目录两份：本地 `localhost:8100/courses/{id}/` 会回传 quiz 进度，发布目录（`/published`）那份是静态副本（可分享、不计进度）。
- 子路径部署走 `.env` 的 `BASE_PATH`（如 `/celestial-snow`）：后端 `_BasePathStrip` 中间件剥前缀（带/不带前缀都能访问），前端 `vite.config.js` 读同一份 `.env` 定构建 base，`api.js` 的 `BASE` 与课程 quiz.js 的相对回传路径（`../../api/…`）自动跟随；改动后须 `cd frontend && npm run build`。
- 技能参数表单：SKILL.md frontmatter 可写 `arguments: {单行 JSON}`（type: text/issue/repo/book/select；条件 `visible_if`: existing_course / existing_repo_course / existing_book_course），web 端据此渲染结构化表单，取值按声明顺序空格拼进 args。词表两端同步：`app/services/skill_runner.py::_parse_arguments` 与 `App.vue` 的 paramVisible。
- 列表分页一律服务端 `limit/offset` + 返回 `total`，排序末尾补 `id` tiebreaker（OFFSET 分页要求全序稳定）；`issues.fixed_hint` 是写入时算好的物化列（摄入/LLM 回写各节点经 `scoring.refresh_fixed_hint` 重算），不要回到「取一批再 Python 排序」的老路。
  fixed_hint 汇总三类信号取或：文本正则（`FIXED_HINT_RE`）｜GitHub 已关闭（`state=closed`）｜修复 PR 已合并（`fixed_by_pr`）。刷新/贡献分析在 open 抓取落库后跑 `_refresh_issue_states`：
  库内仍标 open 但本次抓取缺席的，拉「最近关闭」清单核对（缺席≠关闭，open 超 100 条会掉窗口，以 closed 清单命中为准）；深读顺带用 search 检索「已合并且正文提到该 issue」的修复 PR（`GitHubClient.search_merged_fix_prs`——别改回 timeline：实测带 token 时 timeline 会隐去 cross-referenced 事件，匿名才有）。
- 行业/分类标签统一存 `repos.tags`（JSON 数组，只增不减取并集）；新表/新列走 `db._migrate` 的 inspect+ALTER 模式。
- 标签库（`tags` 表）是 canonical 唯一登记处：所有写 `repos.tags` 的路径先过 `tags.ensure_tags` 归一——别名精确命中直接映射，
  未命中的 LLM 只做翻译级/缩写级同义合并（拿不准保持独立，宁可多标签不可错并）。canonical 一律中文（专有名词除外），
  英文原词进 aliases。历史脏标签（含紧贴式 `/` 的整串）由启动检测的一次性清洗任务拆串（`tag_cleanup`，成功一次即哨兵永不重跑）。
