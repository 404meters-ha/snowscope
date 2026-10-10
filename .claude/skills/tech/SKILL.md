---
name: tech
description: 从 celestial-snow 情报站选一个 issue，一次性生成三章 teach 风格课程并托管到平台 /courses。
disable-model-invocation: true
arguments: {"issue_id": {"type": "issue", "label": "目标 issue", "required": true}, "replace": {"type": "select", "label": "该 issue 已有课程时", "visible_if": "existing_course", "options": [{"value": "", "label": "另起新课（保留旧课）"}, {"value": "replace", "label": "重新生成（覆盖旧课，连记录与旧文件一起清）"}]}}
---

用户要求你针对情报站里的一个 GitHub issue 生成一门课程。这是一次性批量生成：一门课一次生成全部章节，不是多会话教学。

平台即本服务：`http://localhost:8100`（下文 `$API`）。

## 运行环境（先看你手上有哪套工具，再按下表映射）

本技能在两种环境跑同一套流程：

| 要做的事 | Claude Code（本地，有 Shell） | 平台 SDK（web 表单执行） |
|---|---|---|
| 平台 API（learning-context / 注册课程 / 发布） | `curl` | **PlatformAPI** 工具（POST 带 method+body） |
| 抓 GitHub 代码 / README / issue | `curl` api.github.com | **WebFetch** 工具 |
| 读技能模板（assets/、各 FORMAT 文件） | 直接读 `.claude/skills/tech/` | **ReadFile** 工具（可读 `.claude/skills/` 与 `courses/`） |
| 写课程文件 `courses/{id}/` | 直接写文件 | **WriteFile** 工具（只能写 `courses/` 下） |
| 核对文件清单 | `ls` | **ListDir** 工具 |

## 总流程

本次调用参数（可能为空）：`$ARGUMENTS`

`GET $API/api/learning-context/{issue_id}` 取全上下文 → 研读素材（必要时拉真实代码）→ 设计三章骨架 → `POST $API/api/courses` 注册拿 `course_id` → 写盘 `courses/{course_id}/` → 校验 → 发布 → 给出学习入口。

### 第 0 步 · 确定目标 issue

- 参数为空：取 `$API/api/issues?sort=match&deep_only=true&limit=20`，把候选列给用户（id、repo #number、标题、匹配度、难度），等用户选择后停止。
- 参数非空：第一个词就是 issue_id，直接进入第 1 步；若参数里还带 `replace`，覆盖旧课的选择已由用户做完，第 1 步不要再问。

### 第 1 步 · 取上下文

`curl -s "$API/api/learning-context/{issue_id}"`，一次拿到：

- `issue`：标题/标签/难度/摘要/要做什么/正文节选
- `repo`：仓库元数据
- `analysis`：核心思想、企业案例、LLM 评分、**README 全文缓存**
- `report`：贡献报告对该 issue 的深读段落 `this_issue`（背景、切入方式）与仓库综合判断
- `existing_course`：该 issue 已有的课程

**若 `existing_course` 非空：必须先问用户**——覆盖（重生成，`POST /api/courses` 带 `"replace": true`，平台会连旧课程记录与旧课程目录一起清，不需要你删文件）还是新开（保留旧课，另起新课）。
例外：参数里带了 `replace`（web 表单已代用户选过）就直接走覆盖分支，不要再问。

### 第 2 步 · 研读素材，不许凭参数记忆写课

课程的可信度来自真实材料。README 与 issue 深读已在上下文里；**核心代码路径一章必须落到真实文件**：

1. 取 `https://api.github.com/repos/{owner}/{repo}/contents/{path}`（或 raw.githubusercontent.com）看仓库结构，找到与该 issue 相关的模块文件。
2. 读关键源文件的相关片段，确认：入口在哪、数据怎么流、issue 指的问题出在哪段逻辑。
3. 若 issue 正文（`body_excerpt`）被截断，取 `https://api.github.com/repos/{owner}/{repo}/issues/{number}` 拉全文。
4. 课上所有代码片段必须来自你实际拉到的文件，并附 GitHub 深链（blob 链接可锚定行号）。拉不到的宁可写「路径与判断方法」也不编造代码。

### 第 3 步 · 设计课程骨架

固定三章，每章 1–3 课，总 3–9 课，按仓库与 issue 规模自适应，全部中文：

| 章 | 主题 | 教什么 |
|---|---|---|
| 一 | 项目导览 | 这个项目解决什么问题、核心思想、架构鸟瞰 |
| 二 | 核心代码路径 | 与该 issue 相关的模块：入口 → 数据流 → 关键函数 |
| 三 | issue 实战 | 该 issue 的来龙去脉、复现思路、修复方向、相关测试怎么写 |

课程理念（承自 teach，必须遵守）：每课**短**、只给**单一收获**；关键论断给引用链接；宁短勿水。纯知识课——不要教「如何提 PR」（PR 不归平台管）。

课程文件命名：`01-overview.html`、`02-architecture.html`…（两位序号-短横线英文 slug）；`lesson_id` = 去掉 `.html` 的文件名。

### 第 4 步 · 注册课程拿 course_id

```bash
curl -s -X POST "$API/api/courses" -H "Content-Type: application/json" -d '{
  "issue_id": 123,
  "title": "课程标题（中文）",
  "lessons": [
    {"lesson_id": "01-overview", "title": "第 1 课标题", "file": "01-overview.html", "quiz_count": 4},
    {"lesson_id": "02-core-path", "title": "第 2 课标题", "file": "02-core-path.html", "quiz_count": 5}
  ]
}'
```

返回 `{"course_id": N}`。**course_id 必须写死进之后生成的每个 HTML**。副作用：issue 自动进入 `learning`。

### 第 5 步 · 写盘 `courses/{course_id}/`

```
courses/{course_id}/
├── index.html            课程首页（进度 + 目录）
├── assets/
│   ├── style.css         ← 从本 skill 的 assets/ 原样复制（SDK 模式：ReadFile 读出后 WriteFile 原样写入，不要重写内容）
│   └── quiz.js           ← 同上
├── 01-….html … NN-….html 各课
├── MISSION.md
├── RESOURCES.md
├── reference/            按需：术语表、代码路径速查（GLOSSARY-FORMAT.md）
└── learning-records/     骨架 + 0001 初始记录（LEARNING-RECORD-FORMAT.md）
```

- **HTML 结构**：按本 skill 目录下 `LESSON-TEMPLATE.html` / `INDEX-TEMPLATE.html` 的结构契约写（样式引用、quiz 嵌入、课程 id），内容全部重写，不要留占位符。
- **index.html**：`window.COURSE_ID = N` 写死；quiz.js 会自动从平台拉各课 quiz 进度。
- **每课尾部嵌随堂检测**：`<div id="quiz"></div>` + `<script id="quiz-spec" type="application/json">{…}</script>` + `<script src="assets/quiz.js"></script>`。spec 见 quiz.js 头注释；**3–5 题客观题**（单选/多选/判断混合），考本课单一收获；`course_id`、`lesson_id` 写死。判分与回传由组件完成，你只写题。
  - 出题纪律（承自 teach）：各选项字数尽量一致，不得用排版泄露答案；每题带一句 `explain`。
  - lesson_id、quiz_count 必须与第 4 步注册的 lessons 完全一致，否则进度对不上。
- **MISSION.md**：使命 = 彻底理解 `{repo}`、吃透 issue #{number}（格式见 MISSION-FORMAT.md，按此使命填写）。
- **RESOURCES.md**：本课引用过的真实来源（README、issue、关键源文件、官方文档），按 RESOURCES-FORMAT.md。
- **learning-records/**：写 `0001-<slug>.md` 初始记录，概述这门课覆盖了什么、issue 的核心结论（格式见 LEARNING-RECORD-FORMAT.md）。

### 第 6 步 · 校验

1. 列 `courses/{course_id}/` 目录核对文件齐全、lessons 数与注册一致。
2. 取 `$API/api/courses/{course_id}` 核对元数据。

### 第 7 步 · 发布到服务器目录

**每次生成课程都要发布**——本地课程目录不是终点，发布目录里要有可分享的一份。

对 `$API/api/courses/{course_id}/publish` 发 POST（SDK 模式用 PlatformAPI）。

平台负责（你不需要手工改名或改链接）：

- 整门课进独立文件夹 `{course_id}-{repo}-issue{编号}-{课程标题}/`，落盘到服务器 `LOCAL_PUBLISH_DIR`（默认 `./published`，由平台在 `/published` 静态托管）；
- 课件文件用注册时的中文课标题命名（`01-overview.html` → `01-项目导览：….html`，`index.html` → `00-课程目录.html`）；
- 页面之间的相对链接同步改写，发布目录里导航照样可点；
- 静态副本注入 `window.CELESTIAL_PUBLISHED`，`quiz.js` 据此说明成绩不计入平台进度（发布页面调不到平台 API）。

返回 JSON 里的 `entry_url` 就是可分享的入口，`files[].url` 是每个文件的直链（直接发人即可）。

- 重新生成课程后重发，加 `-d '{"prune": true}'` 清掉上一版残留文件。
- 失败会如实返回错误（写入失败是 502，多为权限/磁盘空间问题），**照实转告用户**，不要当成已发布。

### 第 8 步 · 交付

给用户两个入口：

1. 平台内：`$API/courses/{course_id}/index.html`（前端「📚 学习」tab 里也会出现这门课，quiz 会回传进度）；
2. 发布目录：第 7 步返回的 `entry_url`（静态副本、可分享，quiz 成绩不记录）。

## 已明确移除的 teach 能力

多会话有状态教学、ZPD 逐节推进、NOTES.md 偏好沉淀、社区/智慧模块、开课首问使命（使命由 issue 决定，直接写入 MISSION.md）。保留的资产与理念：MISSION / RESOURCES / reference / learning-records / assets 共享组件、短课单一收获、引用规范、出题纪律。原 `~/.claude/skills/teach` 不受影响。
