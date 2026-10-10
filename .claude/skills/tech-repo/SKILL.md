---
name: tech-repo
description: 从情报站选一个 GitHub 项目，一次性生成三章项目课程（项目导览/架构与核心模块/上手与贡献路径）并托管到平台 /courses。
disable-model-invocation: true
arguments: {"repo": {"type": "repo", "label": "目标项目", "required": true}, "focus": {"type": "text", "label": "侧重模块（可空）", "placeholder": "如：调度器、API 层、存储引擎……"}}
---

用户要求你针对情报站里的一个 GitHub **项目**（整体，不针对某个 issue）生成一门课程。
这是一次性批量生成：一门课一次生成全部章节，不是多会话教学。

平台即本服务：`http://localhost:8100`（下文 `$API`）。

## 运行环境（先看你手上有哪套工具，再按下表映射）

本技能在两种环境跑同一套流程：

| 要做的事 | Claude Code（本地，有 Shell） | 平台 SDK（web 表单执行） |
|---|---|---|
| 平台 API（learning-context / 注册课程 / 发布） | `curl` | **PlatformAPI** 工具（POST 带 method+body） |
| 抓 GitHub 代码 / README | `curl` api.github.com | **WebFetch** 工具 |
| 读技能模板（assets/、各 FORMAT 文件） | 直接读 `.claude/skills/tech/` | **ReadFile** 工具（可读 `.claude/skills/` 与 `courses/`） |
| 写课程文件 `courses/{id}/` | 直接写文件 | **WriteFile** 工具（只能写 `courses/` 下） |
| 核对文件清单 | `ls` | **ListDir** 工具 |

> 模板与共享组件**全部复用 tech 技能的**：`.claude/skills/tech/`（LESSON-TEMPLATE.html、
> INDEX-TEMPLATE.html、assets/style.css、assets/quiz.js、各 FORMAT 文件）。本技能目录不另存一份。

## 总流程

本次调用参数（可能为空）：`$ARGUMENTS`

`GET $API/api/learning-context/repo/{repo_id}` 取全上下文 → 研读素材（README 已在上下文，代码现抓）→
设计三章骨架 → `POST $API/api/courses`（`source_type: "repo"`）注册拿 `course_id` →
写盘 `courses/{course_id}/` → 校验 → 发布 → 给出学习入口。

### 第 0 步 · 确定目标项目

- 参数为空：取 `$API/api/repos?sort=total&analyzed=done&limit=20`，把候选列给用户
  （id、full_name、中文简介、语言、总分、标签），等用户选择后停止。
- 参数非空：第一个词就是 repo_id，直接进入第 1 步；若参数还带其他文字，作为「侧重模块」提示（focus）。

### 第 1 步 · 取上下文

`curl -s "$API/api/learning-context/repo/{repo_id}"`（SDK 模式用 PlatformAPI），一次拿到：

- `repo`：仓库元数据（full_name/language/topics/stars/贡献者数…）
- `analysis`：core_idea、enterprise_cases、LLM 评分、**README 全文缓存**
- `report`：最近一份贡献报告的 `repo_verdict`（综合判断）与 `stats`
- `top_issues`：该仓匹配度最高的 5 个 issue（上手路径章的素材）
- `existing_courses`：该项目已有的项目课（存在时不阻塞，直接另起新课；本项目课**不支持 replace 覆盖**）

### 第 2 步 · 研读素材，不许凭参数记忆写课

课程的可信度来自真实材料。README 与精析已在上下文里；**架构与核心模块章必须落到真实代码**：

1. 取 `https://api.github.com/repos/{owner}/{repo}/contents/` 逐层浏览目录结构，识别核心模块
   （结合 focus 参数与 README 里声明的架构）。深度 2-3 层即可，别漫游全仓。
2. 挑 4-6 个关键源文件读相关片段（raw.githubusercontent.com）：入口、路由/CLI 注册、
   核心数据结构、focus 指向的模块。确认：程序从哪启动、数据怎么流、扩展点在哪。
3. 课上所有代码片段必须来自你实际拉到的文件，并附 GitHub 深链（blob 链接可锚定行号）。
   拉不到的宁可写「路径与判断方法」也不编造代码。

### 第 3 步 · 设计课程骨架

固定三章，每章 1-3 课，总 3-9 课，按项目规模自适应，全部中文：

| 章 | 主题 | 教什么 |
|---|---|---|
| 一 | 项目导览 | 解决什么问题、核心思想、架构鸟瞰（目录结构 → 模块职责表） |
| 二 | 架构与核心模块走读 | 挑 1-2 条主线走读：入口 → 数据流 → 关键抽象；focus 有值时优先它 |
| 三 | 上手路径 | 环境搭建与本地运行 → 从 top_issues 挑 2-3 个适合上手的点 → 贡献流程与规范 |

课程理念（承自 teach，必须遵守）：每课**短**、只给**单一收获**；关键论断给引用链接；宁短勿水。

课程文件命名：`01-overview.html`、`02-architecture.html`…（两位序号-短横线英文 slug）；
`lesson_id` = 去掉 `.html` 的文件名。

### 第 4 步 · 注册课程拿 course_id

```bash
curl -s -X POST "$API/api/courses" -H "Content-Type: application/json" -d '{
  "source_type": "repo",
  "repo_id": 123,
  "title": "课程标题（中文，建议带仓库名）",
  "lessons": [
    {"lesson_id": "01-overview", "title": "第 1 课标题", "file": "01-overview.html", "quiz_count": 4},
    {"lesson_id": "02-core-path", "title": "第 2 课标题", "file": "02-core-path.html", "quiz_count": 5}
  ]
}'
```

返回 `{"course_id": N}`。**course_id 必须写死进之后生成的每个 HTML**。issue 课才有的
`learning` 状态联动对项目课不生效，不用管。

### 第 5 步 · 写盘 `courses/{course_id}/`

目录结构与文件规范和 /tech 完全一致（模板从 **`.claude/skills/tech/`** 读）：

```
courses/{course_id}/
├── index.html            课程首页（进度 + 目录），window.COURSE_ID = N 写死
├── assets/
│   ├── style.css         ← ReadFile 读 .claude/skills/tech/assets/style.css 后原样写入
│   └── quiz.js           ← 同上
├── 01-….html … NN-….html 各课（尾部嵌 quiz：3-5 题客观题 + explain，spec 见 quiz.js 头注释）
├── MISSION.md            使命 = 吃透 {repo} 这个项目（格式：.claude/skills/tech/MISSION-FORMAT.md）
├── RESOURCES.md          引用过的真实来源（README、关键源文件、官方文档）
└── learning-records/     0001 初始记录（LEARNING-RECORD-FORMAT.md）
```

lesson_id、quiz_count 必须与第 4 步注册的 lessons 完全一致，否则进度对不上。

### 第 6 步 · 校验

1. ListDir `courses/{course_id}/` 核对文件齐全、lessons 数与注册一致。
2. 取 `$API/api/courses/{course_id}` 核对元数据。

### 第 7 步 · 发布到服务器目录

**每次生成课程都要发布**。对 `$API/api/courses/{course_id}/publish` 发 POST（SDK 模式用 PlatformAPI）。
平台负责分文件夹（`{course_id}-{repo}-{课程标题}/`）、中文课标题改名、相对链接改写、注入静态副本标记。
失败会如实返回错误，**照实转告用户**，不要当成已发布。

### 第 8 步 · 交付

给用户两个入口：

1. 平台内：`$API/courses/{course_id}/index.html`（前端「📚 学习」tab 里也会出现这门课，quiz 回传进度）；
2. 发布目录：第 7 步返回的 `entry_url`（静态副本、可分享，quiz 成绩不记录）。
