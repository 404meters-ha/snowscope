---
name: tech-book
description: 从上传的电子书教材（PDF 已解析成逐页文本）生成教程课程：整本精讲（挑重点章）或单章精读（章节系列），托管到平台 /courses。
disable-model-invocation: true
arguments: {"book": {"type": "book", "label": "目标教材", "required": true}, "focus": {"type": "text", "label": "聚焦（可空）", "placeholder": "章节号或主题，如：第3章 / 论文建模 / 计算部分"}}
---

用户要求你基于一本**电子书教材**（PDF，平台已解析成逐页文本）生成一门教程课程。
这是一次性批量生成：一门课一次生成全部章节，不是多会话教学。

平台即本服务：`http://localhost:8100`（下文 `$API`）。

## 运行环境（先看你手上有哪套工具，再按下表映射）

本技能在两种环境跑同一套流程：

| 要做的事 | Claude Code（本地，有 Shell） | 平台 SDK（web 表单执行） |
|---|---|---|
| 平台 API（learning-context / 注册课程 / 发布） | `curl` | **PlatformAPI** 工具（POST 带 method+body） |
| 读教材页文本 uploads/books/{id}/text/ | 直接读文件 | **ReadFile** 工具（uploads/ 在白名单内） |
| 读技能模板（assets/、各 FORMAT 文件） | 直接读 `.claude/skills/tech/` | **ReadFile** 工具 |
| 写课程文件 `courses/{id}/` | 直接写文件 | **WriteFile** 工具（只能写 `courses/` 下） |
| 核对文件清单 | `ls` | **ListDir** 工具 |

> 模板与共享组件**全部复用 tech 技能的**：`.claude/skills/tech/`（LESSON-TEMPLATE.html、
> INDEX-TEMPLATE.html、assets/style.css、assets/quiz.js、各 FORMAT 文件）。本技能目录不另存一份。

## 总流程

本次调用参数（可能为空）：`$ARGUMENTS`

`GET $API/api/learning-context/book/{book_id}` 取大纲 → 定模式（整本精讲 / 单章精读）→
读所选章节的页文本 → 设计骨架 → `POST $API/api/courses`（`source_type: "book"`）注册拿 `course_id` →
写盘 `courses/{course_id}/` → 校验 → 发布 → 给出学习入口。

### 第 0 步 · 确定教材与模式

- 参数为空：取 `$API/api/books`，把已解析完成的教材列给用户（id、书名、页数、章数），等用户选择后停止。
- 参数第一个词是 book_id。
- **单章模式**：参数里还带「第N章」（如 `5 第3章`）——只精讲该章，全程**不要问用户任何问题**
  （这是「章节系列课」的后台逐章调用，一章一门课，无人值守）。
- 其余参数文字作为「聚焦」（focus）：整本精讲时优先挑与它相关的章。

### 第 1 步 · 取上下文

`GET $API/api/learning-context/book/{book_id}`（SDK 模式用 PlatformAPI），一次拿到：

- `book`：书名、总页数、解析统计（`stats.vision_pages` 大说明是扫描版转录，引用时留意可能有识别误差）
- `chapters`：学习大纲，每章 `{no, title, start, end, summary, why, files}`——`files` 是该章逐页文本文件清单
- `text_dir`：页文本目录（`uploads/books/{book_id}/text/`），文件名 `pNNNN.txt` 的 NNNN 即页码
- `existing_courses`：这本教材已有的课（存在时不阻塞，直接另起新课；教材课**不支持 replace 覆盖**）

### 第 2 步 · 读素材，不许凭书名常识写课

课程的可信度来自教材原文。**每个要写进课的论断都必须出自你实际读到的页文本**：

1. 先读所选章的 `summary`（大纲里有），再 ReadFile 该章 `files` 里的页文本。一章几十页不必逐页全读：
   开头几页（章引入）+ 按小节标题抽样 + 关键概念页精读；上下文预算紧就跳读。
2. **引用一律带页码**：正文写成「（教材 p.123）」这样的标注——文件名就是页码，读者可回书核对。
3. 扫描版转录（stats.vision_pages 占比高）可能出现错字/公式变形，转述时不照抄可疑片段，用通顺语言重写并保留页码。
4. focus 有值时，优先挑与它直接相关的章/小节，其余从简。

### 第 3 步 · 设计课程骨架

**整本精讲**（参数无章号）：挑 3-6 个重点章（结合 `why` 字段与用户画像），三章结构：

| 章 | 主题 | 教什么 |
|---|---|---|
| 一 | 全书导览 | 这本书讲什么、知识结构地图（章 → 页范围）、适合谁、怎么学 |
| 二 | 重点章精读 | 每个重点章 1-2 课：核心概念、关键方法、与相邻章的关系 |
| 三 | 考点与自测 | 全书重点归纳、易混概念辨析、（考证教材则）题型与答题要点 |

**单章精读**（参数带「第N章」）：3-6 课只讲该章：

| 课 | 教什么 |
|---|---|
| 章导览 | 本章在大纲里的位置、要解决的问题、知识地图 |
| 核心内容 1-3 课 | 按小节展开：概念 → 方法 → 例子，全部带页码引用 |
| 考点自测 | 本章重点归纳 + 易混点 + 真题风格 quiz |

课程理念（承自 teach，必须遵守）：每课**短**、只给**单一收获**；关键论断给**页码引用**；宁短勿水。
课程文件命名：`01-overview.html`、`02-ch{N}-….html`…（两位序号-短横线英文 slug）；
`lesson_id` = 去掉 `.html` 的文件名。

### 第 4 步 · 注册课程拿 course_id

```bash
curl -s -X POST "$API/api/courses" -H "Content-Type: application/json" -d '{
  "source_type": "book",
  "book_id": 5,
  "title": "书名·第3章 章题（单章）/ 书名·精讲（整本）",
  "lessons": [
    {"lesson_id": "01-overview", "title": "第 1 课标题", "file": "01-overview.html", "quiz_count": 4},
    {"lesson_id": "02-ch3-models", "title": "第 2 课标题", "file": "02-ch3-models.html", "quiz_count": 5}
  ]
}'
```

返回 `{"course_id": N}`。**course_id 必须写死进之后生成的每个 HTML**。

### 第 5 步 · 写盘 `courses/{course_id}/`

目录结构与文件规范和 /tech 完全一致（模板从 **`.claude/skills/tech/`** 读）：

```
courses/{course_id}/
├── index.html            课程首页（进度 + 目录），window.COURSE_ID = N 写死
├── assets/
│   ├── style.css         ← ReadFile 读 .claude/skills/tech/assets/style.css 后原样写入
│   └── quiz.js           ← 同上
├── 01-….html … NN-….html 各课（尾部嵌 quiz：3-5 题客观题 + explain，spec 见 quiz.js 头注释）
├── MISSION.md            使命 = 吃透《书名》（整本/第N章）（格式：.claude/skills/tech/MISSION-FORMAT.md）
├── RESOURCES.md          本课引用的页码清单（按章归组：章 → 页码 → 对应课）
└── learning-records/     0001 初始记录（LEARNING-RECORD-FORMAT.md）
```

lesson_id、quiz_count 必须与第 4 步注册的 lessons 完全一致，否则进度对不上。

### 第 6 步 · 校验

1. ListDir `courses/{course_id}/` 核对文件齐全、lessons 数与注册一致。
2. 取 `$API/api/courses/{course_id}` 核对元数据。

### 第 7 步 · 发布到服务器目录

**每次生成课程都要发布**。对 `$API/api/courses/{course_id}/publish` 发 POST（SDK 模式用 PlatformAPI）。
平台负责分文件夹（`{course_id}-教材-{课程标题}/`）、中文课标题改名、相对链接改写、注入静态副本标记。
失败会如实返回错误，**照实转告用户**，不要当成已发布。

### 第 8 步 · 交付

给用户两个入口：

1. 平台内：`$API/courses/{course_id}/index.html`（前端「📚 学习」tab 里也会出现这门课，quiz 回传进度）；
2. 发布目录：第 7 步返回的 `entry_url`（静态副本、可分享，quiz 成绩不记录）。

单章模式（系列课后台调用）最后只需一行汇报：课程标题 + course_id + 入口 URL。
