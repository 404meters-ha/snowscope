"""LLM provider：OpenAI 兼容 chat 接口（GLM coding plan 等），JSON 输出解析。

未配置 key 时抛 LLMNotConfigured，由上层决定降级（榜单只显示规则分，不阻塞抓取）。
"""
import asyncio
import base64
import json
import re

import httpx

from ..config import get_settings


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base_url = s.llm_base_url.rstrip("/")
        self._api_key = s.llm_api_key
        self._model = s.llm_model
        self._vision_model = s.llm_vision_model
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=120,
        )

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, system: str, user: str, *, max_tokens: int = 4000) -> str:
        """单轮对话，返回助手文本。

        glm-4.7 等思考模型会把推理链写进 reasoning_content 并挤占 max_tokens，
        极端时 content 为空（finish_reason=length）——提取类任务一律关掉思考；
        端点不认 thinking 字段时自动退回普通请求。仍超长则翻倍 max_tokens 重试一次。
        """
        if not self.configured:
            raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY）")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        data = await self._post_chat(messages, max_tokens, thinking=False)
        if not (data.get("choices") or [{}])[0].get("message", {}).get("content"):
            # 空响应：翻倍额度再试一次（仍不带思考）
            data = await self._post_chat(messages, max_tokens * 2, thinking=False)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"LLM 响应结构异常: {e}") from e

    async def chat_vision(self, system: str, prompt: str, image_png: bytes, *, max_tokens: int = 8000) -> str:
        """多模态单轮：一张 PNG 图片 + 文字指令（扫描版 PDF 逐页转录用）。

        走 OpenAI 兼容的 image_url 消息（data URL base64）；模型用 LLM_VISION_MODEL
        （flash 档便宜量大，逐页转录成本可控），空则回退主模型。失败重试一次。
        """
        if not self.configured:
            raise LLMNotConfigured("LLM 未配置（LLM_BASE_URL / LLM_API_KEY）")
        b64 = base64.b64encode(image_png).decode()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]},
        ]
        model = self._vision_model or self._model
        last_err: Exception | None = None
        for _ in range(2):  # 视觉端点偶发 5xx，重试一次
            try:
                data = await self._post_chat(messages, max_tokens, thinking=False, model=model)
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if content:
                    return content
            except LLMError as e:
                last_err = e
        raise LLMError(f"视觉模型转录失败（{model}）：{last_err or '空响应'}")

    async def _post_chat(self, messages: list[dict], max_tokens: int, *, thinking: bool,
                         model: str | None = None) -> dict:
        payload: dict = {"model": model or self._model, "messages": messages, "max_tokens": max_tokens}
        if thinking is False:
            payload["thinking"] = {"type": "disabled"}
        # coding plan 端点偶发掐连接（Server disconnected without sending a response，
        # 并发任务同打 LLM 时尤甚）与 429/5xx：传输层错误与这两类状态码退避重试；
        # 400/401/403 等请求侧错误重试无意义，直接抛。
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except httpx.TransportError as e:
                last_err = e
                await asyncio.sleep(attempt + 1)
                continue
            if resp.status_code == 400 and "thinking" in resp.text and payload.get("thinking"):
                return await self._post_chat(messages, max_tokens, thinking=True, model=model)  # 旧端点不认该字段
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
                await asyncio.sleep(attempt + 1)
                continue
            if resp.status_code != 200:
                raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise LLMError(f"LLM 调用重试 3 次仍失败：{last_err}")

    async def chat_json(self, system: str, user: str, *, max_tokens: int = 4000) -> dict:
        """要求 JSON 输出并解析；模型偶尔包 ```json 围栏或夹带说明文字，逐一兜住。"""
        text = await self.chat(system, user, max_tokens=max_tokens)
        return parse_json_object(text)


def parse_json_object(text: str) -> dict:
    """从 LLM 输出中提取第一个平衡的 JSON 对象。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        cleaned = cleaned[newline + 1 :] if newline >= 0 else ""
        end = cleaned.rfind("```")
        if end >= 0:
            cleaned = cleaned[:end]
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 首个平衡的 {...}
    start = cleaned.find("{")
    if start < 0:
        raise LLMError(f"LLM 输出中没有 JSON: {text[:200]}")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    break
    raise LLMError(f"LLM 输出 JSON 无法解析: {re.sub(chr(10), ' ', text[:200])}")


# ---------- 领域 prompt ----------

ANALYZE_SYSTEM = """你是开源项目情报分析师。分析 GitHub 仓库的 README，输出严格 JSON（不要 markdown 围栏）：
{
  "core_idea": "这个项目要解决什么问题、核心思想是什么（150字内）",
  "enterprise_cases": [
    {"company": "公司/组织名", "scenario": "在什么场景用", "evidence": "README/官网原句（逐字引用）"}
  ],
  "enterprise_potential": {"score": 0-100, "reason": "评分理由（80字内）"},
  "match": {"score": 0-100, "reason": "与用户画像的匹配度及理由（80字内）"},
  "learning_value": {"score": 0-100, "reason": "核心思想/学习价值及理由（80字内）"}
}
enterprise_cases 只收 README/官网中**明确声明**的生产使用案例（如 "used by X in production"、官方用户列表）；
没有确凿证据就返回空数组，绝不凭记忆编造公司名。"""

PROFILE_NOTE = "用户画像（用于 match 维度）：{profile}"


async def analyze_repo(llm: LLMClient, profile: str, full_name: str, readme: str) -> dict:
    """LLM 精析单个仓库：核心思想 + 企业案例（保守提取）+ 三维评分。"""
    user = PROFILE_NOTE.format(profile=profile) + f"\n\n仓库：{full_name}\n\nREADME（截断）：\n{readme[:12000]}"
    return await llm.chat_json(ANALYZE_SYSTEM, user)


SCREEN_SYSTEM = """你是开源贡献机会筛选器。给定仓库信息、用户技能清单和 open issues 列表（JSON），挑出**适合外部贡献者**参与的 issue 并打匹配分。
排除：核心维护者的内部任务、缺复现步骤且无人回应的、需要深度领域知识而外部人无法上手的。
输出严格 JSON（不要 markdown 围栏）：
{"picked": [{"number": 123, "match": 0-100, "difficulty": "低|中|高", "reason": "为什么适合这位开发者（50字内）"}]}
match 是「与用户技能方向的贴合度 + 外部贡献者上手可行性」综合分；最多挑 8 个；一个都没有就返回 {"picked": []}。"""

REPORT_SYSTEM = """你是开源贡献导师，面向一位「后端为主、正在扩展 AI 应用方向」的开发者。对单个 issue 输出深度解读，严格 JSON（不要 markdown 围栏）：
{"number": 123,
 "title": "原标题",
 "url": "issue 链接",
 "labels": ["标签"],
 "background": "这个 issue 的来龙去脉（120字内）",
 "why_it_matters": "为什么值得修/是坑（100字内）",
 "modules": "预计涉及的代码模块/目录（按仓库结构推测）",
 "difficulty": "低|中|高",
 "approach": "建议切入方式（120字内，具体可执行）",
 "fit_reason": "为什么适合这位开发者"}
全部中文。"""

VERDICT_SYSTEM = """你是开源贡献顾问。基于仓库概况、已挑选的 issue 清单和 CONTRIBUTING 情况，给出这个仓库值不值得投入的综合判断，严格 JSON（不要 markdown 围栏）：
{"verdict": "综合判断（150字内）",
 "worth_investing": true,
 "directions": ["除这些 issue 外的长期贡献方向建议"],
 "health": "维护活跃度/社区友好度/对新人态度的评价（100字内）"}
全部中文。"""

SUMMARIZE_SYSTEM = """你是 issue 摘要器。对输入的每个 GitHub issue 输出一句话摘要和一句话行动建议，严格 JSON（不要 markdown 围栏）：
{"items": [{"number": 123,
  "summary": "这个 issue 说了个什么事（≤50字，具体到现象/需求本身，不要套话）",
  "action": "需要贡献者做什么（≤50字，具体可执行的下一步）"}]}
必须覆盖输入里的每一个 number；正文太短看不出所以然的，summary 写标题的展开、action 写「阅读 issue 并向维护者确认复现步骤」这类合理下一步。全部中文。"""


async def summarize_issues(llm: LLMClient, issues: list[dict]) -> dict[int, dict]:
    """批量摘要：输入 [{number, title, body}]，返回 {number: {summary, action}}。"""
    if not issues:
        return {}
    user = json.dumps(
        [{"number": i["number"], "title": i.get("title", ""), "body": (i.get("body") or "")[:600]} for i in issues],
        ensure_ascii=False,
    )
    data = await llm.chat_json(SUMMARIZE_SYSTEM, user, max_tokens=3000)
    return {item["number"]: item for item in data.get("items", []) if item.get("number") is not None}


# ---------- 定向行业分析 ----------

PARSE_INPUT_SYSTEM = """你是开源项目情报站的输入解析器。用户在行业分析框里输入一段自由文本，其中可能混合「技术方向」和「具体项目名」。把它拆解开，输出严格 JSON（不要 markdown 围栏）：
{
  "directions": [{"raw": "原文片段", "canonical": "规范中文方向名（专有名词除外）"}],
  "repos": [{"raw": "原文片段", "full_name": "你确信的 owner/repo；不确定就给空字符串"}]
}
判断规则：
- 技术方向：一类技术/行业的统称（agent运行时、oauth2、向量数据库），平台会围绕它做行业调研 → 进 directions
- 具体项目名：某个 GitHub 仓库的名字或名字片段（pi、agentScope-java、deer-flow）→ 进 repos；
  结合语境判断——"pi" 单独出现难说，出现在 "agent运行时" 旁边大概率是项目名
- canonical 优先复用【标签库】里的已有写法（意思相同就原样用，避免同义标签）；库里没有才新拟
- 方向 0-3 个、项目 0-5 个；实在两不像的片段归 directions（默认用户想调研它）"""

NORMALIZE_SYSTEM = """你是标签词表管理员。给定新词清单和现有标签库，判断哪些新词是某个现有标签的同义说法。只做两类合并：
1. 纯翻译级同义：中英对照（"AI" vs "人工智能"、"Agent 框架" vs "agent framework"）
2. 明显缩写全称："LLM" vs "大语言模型"
其余一律不合并——字面相近但含义有别（"推理框架" vs "推理引擎"）、粒度不同（"AI" vs "AI 视频生成"）都必须保持独立。
输出严格 JSON（不要 markdown 围栏）：{"words": [{"word": "新词", "tag": "现有标签名"}]}
只报你非常确定的（思考后仍有犹豫就不报）；一个都不可并就返回 {"words": []}。"""

INDUSTRY_PLAN_SYSTEM = """你是开源领域情报专家。用户给出一个行业/技术方向词，你负责规划一次开源项目调研，输出严格 JSON（不要 markdown 围栏）：
{
  "definition": "这个行业/方向是什么、当前处于什么阶段（80字内）",
  "sub_categories": ["按技术栈或应用场景切分的子方向，6-10个，中文"],
  "search_keywords": ["GitHub 搜索用的英文关键词/短语，6-10个，覆盖面要广（含通用词与专有名词）"],
  "flagship_repos": ["你确信存在的代表项目 full_name（owner/repo），8-15个；不确定的不要写"]
}
search_keywords 用于 GitHub 搜索，必须是英文；sub_categories 用中文。flagship_repos 只写你非常确定的（如生成视频方向的 comfyanonymous/ComfyUI），宁缺毋滥。"""

INDUSTRY_REPORT_SYSTEM = """你是开源行业分析师。基于给定的行业方向、调研规划和候选项目清单（含 GitHub 实测数据），输出一份行业开源格局报告，严格 JSON（不要 markdown 围栏）：
{
  "overview_md": "行业综述 Markdown：## 行业概览（这是什么、发展阶段、驱动因素）、## 开源格局（各子方向代表项目与竞争态势，可加 ### 子标题）、## 与用户的契合点（结合用户画像，2-3条）、## 建议关注顺序（结合成熟度与用户方向）。全文 500-800 字",
  "projects": [
    {"full_name": "owner/repo（必须与候选清单完全一致，不得新增或改写）",
     "category": "所属子方向（从规划的 sub_categories 中选，可自拟更贴切的中文短语）",
     "position": "一句话定位：它在这行当里扮演什么角色、特色是什么（40字内）"}
  ]
}
projects 必须覆盖候选清单里值得关注的全部项目（可剔除明显不相关的）；全部中文（full_name 除外）。"""

TAXONOMY_SYSTEM = """你是开源项目分类专家。给定一批 GitHub 项目（名称/描述/话题/语言），提出一套覆盖它们的中文行业分类体系，输出严格 JSON（不要 markdown 围栏）：
{"categories": ["分类名，8-15个，粒度适中（如：AI 视频生成、LLM 应用框架、数据湖、前端框架、DevOps 工具），相互尽量不重叠"]}
分类要让任何一个项目都能找到归属，可含一个「其他」兜底（分类名不要带斜杠）。
若提供了【已有标签库】，意思相同的分类必须原样复用库内写法（禁止另造同义新词，如库里有「人工智能」就不要再提「AI 技术」）；库覆盖不到的才新拟。"""

CLASSIFY_SYSTEM = """你是开源项目分类器。给定候选分类和一批项目，给每个项目打 1-3 个最贴切的分类标签，输出严格 JSON（不要 markdown 围栏）：
{"items": [{"full_name": "owner/repo", "tags": ["从候选分类中选，1-3个；确实都不合适才用「其他」"]}]}
必须覆盖输入的每一个 full_name；tags 只能从候选分类里选。"""

TRANSLATE_SYSTEM = """你是开源项目简介翻译器。给定一批 GitHub 项目（名称/原始描述/语言/话题），为每个项目写一句中文简介：它到底是干什么的。严格 JSON（不要 markdown 围栏）：
{"items": [{"full_name": "owner/repo（原样回显，不得改写）",
  "zh": "中文一句话简介，≤60字：直说它解决什么问题/给谁用，不用营销腔、不堆形容词"}]}
必须覆盖输入的每一个 full_name。描述是英文的翻译并提炼；描述缺失或太短的，结合项目名、话题和你的知识推断；实在无从判断就按项目名字面意思简要说明。"""


async def translate_descriptions(llm: LLMClient, repos: list[dict]) -> dict[str, str]:
    """批量生成中文简介：输入 [{full_name, description, language, topics}]，返回 {full_name: zh}。"""
    user = json.dumps(repos, ensure_ascii=False)
    data = await llm.chat_json(TRANSLATE_SYSTEM, user, max_tokens=3000)
    return {
        str(item["full_name"]): str(item.get("zh") or "").strip()
        for item in data.get("items", [])
        if item.get("full_name")
    }


async def parse_industry_input(llm: LLMClient, text: str, known_tags: list[str]) -> dict:
    """自由文本 → {directions: [{raw, canonical}], repos: [{raw, full_name}]}。"""
    user = f"【标签库】{json.dumps(known_tags, ensure_ascii=False)}\n\n用户输入：{text}"
    data = await llm.chat_json(PARSE_INPUT_SYSTEM, user, max_tokens=1000)
    directions = [
        {"raw": str(d.get("raw", "")).strip(), "canonical": str(d.get("canonical", "")).strip() or str(d.get("raw", "")).strip()}
        for d in data.get("directions", []) if str(d.get("raw", "")).strip()
    ]
    repos = [
        {"raw": str(r.get("raw", "")).strip(), "full_name": str(r.get("full_name", "")).strip()}
        for r in data.get("repos", []) if str(r.get("raw", "")).strip()
    ]
    return {"directions": directions, "repos": repos}


async def normalize_tags(llm: LLMClient, words: list[str], existing_tags: list[str]) -> dict[str, str]:
    """新词 → 现有标签的合并映射（只含高置信同义；没判中的词不在结果里，由调用方新建）。"""
    user = (
        f"【现有标签库】{json.dumps(existing_tags, ensure_ascii=False)}\n\n"
        f"【新词清单】{json.dumps(words, ensure_ascii=False)}"
    )
    data = await llm.chat_json(NORMALIZE_SYSTEM, user, max_tokens=1000)
    return {
        str(w["word"]): str(w["tag"])
        for w in data.get("words", [])
        if str(w.get("word", "")).strip() and str(w.get("tag", "")).strip()
    }



async def plan_industry(llm: LLMClient, industry: str, profile: str) -> dict:
    """行业调研规划：定义 + 子方向 + 搜索关键词 + 代表项目。"""
    user = f"行业/方向词：{industry}\n\n{PROFILE_NOTE.format(profile=profile)}"
    return await llm.chat_json(INDUSTRY_PLAN_SYSTEM, user, max_tokens=2000)


async def report_industry(llm: LLMClient, industry: str, profile: str, plan: dict, candidates: list[dict]) -> dict:
    """行业格局报告：overview_md + 每项目分类定位（full_name 必须回显候选清单）。"""
    user = (
        f"行业/方向词：{industry}\n{PROFILE_NOTE.format(profile=profile)}\n\n"
        f"调研规划：{json.dumps({k: plan.get(k) for k in ('definition', 'sub_categories')}, ensure_ascii=False)}\n\n"
        f"候选项目（GitHub 实测数据，{len(candidates)} 个）：\n"
        + json.dumps(candidates, ensure_ascii=False)
    )
    return await llm.chat_json(INDUSTRY_REPORT_SYSTEM, user, max_tokens=6000)


async def propose_taxonomy(llm: LLMClient, repos: list[dict], known_tags: list[str] | None = None) -> list[str]:
    """全量项目的分类体系提案（打标签第一步）。known_tags 非空时优先复用库内已有标签。"""
    note = f"【已有标签库（意思相同的分类必须原样复用）】{json.dumps(known_tags, ensure_ascii=False)}\n\n" if known_tags else ""
    user = note + json.dumps(repos, ensure_ascii=False)
    data = await llm.chat_json(TAXONOMY_SYSTEM, user, max_tokens=1500)
    return [str(c) for c in data.get("categories", []) if str(c).strip()]


async def classify_repos(llm: LLMClient, categories: list[str], repos: list[dict]) -> dict[str, list[str]]:
    """按分类体系给一批项目打标签，返回 {full_name: [tag, ...]}。"""
    user = (
        f"候选分类：{json.dumps(categories, ensure_ascii=False)}\n\n"
        f"项目：\n{json.dumps(repos, ensure_ascii=False)}"
    )
    data = await llm.chat_json(CLASSIFY_SYSTEM, user, max_tokens=3000)
    return {
        str(item["full_name"]): [str(t) for t in item.get("tags", []) if str(t).strip()]
        for item in data.get("items", [])
        if item.get("full_name")
    }


async def screen_issues(llm: LLMClient, full_name: str, contributing: str, issues: list[dict], skills: str) -> list[dict]:
    """LLM 预筛适合外部贡献者的 issue，返回 [{number, match, difficulty, reason}]。"""
    slim = [
        {
            "number": i["number"],
            "title": i.get("title", ""),
            "labels": [lb.get("name", "") for lb in i.get("labels", [])],
            "comments": i.get("comments", 0),
            "body": (i.get("body") or "")[:800],
            "created_at": i.get("created_at", ""),
        }
        for i in issues
    ]
    user = (
        f"仓库：{full_name}\n用户技能：{skills}\n"
        f"CONTRIBUTING 摘要：{contributing[:1500] or '（无）'}\n\n"
        f"open issues（{len(slim)} 个）：\n{json.dumps(slim, ensure_ascii=False)}"
    )
    data = await llm.chat_json(SCREEN_SYSTEM, user, max_tokens=3000)
    return data.get("picked", [])


async def report_issue(
    llm: LLMClient, profile: str, full_name: str, issue: dict, comments: list[dict], contributing: str
) -> dict:
    """单 issue 深度解读。"""
    comment_text = "\n".join(
        f"- {c.get('user', {}).get('login', '?')}: {(c.get('body') or '')[:400]}" for c in comments
    )
    user = (
        f"用户画像：{profile}\n仓库：{full_name}\n"
        f"CONTRIBUTING 摘要：{contributing[:1000] or '（无）'}\n\n"
        f"Issue #{issue['number']} {issue.get('title', '')}\n"
        f"正文：{(issue.get('body') or '')[:2500]}\n\n评论（最近）：\n{comment_text or '（无）'}"
    )
    data = await llm.chat_json(REPORT_SYSTEM, user, max_tokens=2500)
    return data if data else {}


async def repo_verdict(
    llm: LLMClient,
    full_name: str,
    repo_meta: dict,
    picked: list[dict],
    contributing: str,
    contributors_count: int,
) -> dict:
    """仓库综合判断：值不值得投入。"""
    user = (
        f"仓库：{full_name}\n概况：{json.dumps(repo_meta, ensure_ascii=False)}\n"
        f"贡献者数量：{contributors_count}\n"
        f"CONTRIBUTING：{'有' if contributing else '无'}\n"
        f"已挑选的 issue：{json.dumps(picked, ensure_ascii=False)}"
    )
    return await llm.chat_json(VERDICT_SYSTEM, user, max_tokens=1500)


# ---------- 脚手架：需求归纳 / 适配度评分 ----------

SCAFFOLD_BRIEF_SYSTEM = """你是开源选型助手。用户用一句话描述想做的系统，你负责把需求结构化并规划检索关键词，输出严格 JSON（不要 markdown 围栏）：
{
  "domain": "中文领域名（如：待办事项应用、游戏辅助工具、RSS 聚合站）",
  "summary": "需求理解摘要（60字内：这是什么、给谁用、核心价值）",
  "features": ["核心功能点，2-5 个"],
  "scale": "规模判断（个人工具 / 小型服务 / 团队系统 / 平台）",
  "constraints": ["约束（语言偏好、平台、离线等；没有就空数组）"],
  "search_keywords": ["GitHub 搜索用的英文关键词/短语，5-8 个，覆盖面要广（含领域词与技术词）"],
  "tech_hints": ["预计涉及的技术栈关键词（英文小写，如 fastapi, vue, opencv）"]
}
search_keywords 决定检索质量：优先「领域名词」（todo app / game bot / rss reader），补 1-2 个技术词；
不要生僻缩写。全部中文（列表内英文除外）。"""

SCAFFOLD_FIT_SYSTEM = """你是开源选型评估器。给定用户需求（原话+归纳）和候选开源项目清单（GitHub 实测数据），评估每个项目作为该需求**起点**的适配度，输出严格 JSON（不要 markdown 围栏）：
{"items": [{"full_name": "owner/repo（原样回显，不得改写）",
  "score": 0-100,
  "reason": "适配理由（60字内：覆盖了需求的哪些部分、拿来做主干还缺什么）"}]}
score 口径：整体框架级（拿来即可当项目主干）80+；组件级（只覆盖部分需求，需再组装）50-79；
边缘相关（只覆盖单一小块）30-49；不相关 <30。必须覆盖输入的每一个 full_name。全部中文（full_name 除外）。"""


async def scaffold_brief(llm: LLMClient, text: str, profile: str) -> dict:
    """一句话需求 → 结构化归纳 + 检索关键词规划。"""
    user = f"{PROFILE_NOTE.format(profile=profile)}\n\n用户需求：{text}"
    return await llm.chat_json(SCAFFOLD_BRIEF_SYSTEM, user, max_tokens=1500)


async def scaffold_fit_batch(llm: LLMClient, need_text: str, candidates: list[dict],
                             focus: str = "") -> list[dict]:
    """批量适配度评分：输入需求文本 + 精简候选，返回 [{full_name, score, reason}]。
    focus 非空时前置声明评分视角（条目级选组件 ≠ 整体找框架，口径不同）。"""
    user = (
        f"{focus}用户需求：\n{need_text}\n\n"
        f"候选项目（{len(candidates)} 个）：\n{json.dumps(candidates, ensure_ascii=False)}"
    )
    data = await llm.chat_json(SCAFFOLD_FIT_SYSTEM, user, max_tokens=3000)
    return [i for i in data.get("items", []) if str(i.get("full_name", "")).strip()]


SCAFFOLD_ADOPT_SYSTEM = """你是开源选型顾问。用户决定采用某个开源框架作为项目起点，基于需求与项目资料写一份采用评估报告，直接输出 Markdown 正文（不要 JSON、不要代码围栏），固定四节：
## 定位与原理
这个项目是干什么的、核心设计思想（120字内）
## 为什么适配
对照用户需求逐条说明覆盖情况与欠缺（150字内）
## 上手要点
clone 之后的安装/启动/目录结构要点，从提供的 README 提取（150字内）
## 风险与注意
license、活跃度、学习成本、与需求的缺口（100字内）
结论先行，信息以 README 为准——README 里没有的不要编。全部中文。"""


async def scaffold_adopt_report(llm: LLMClient, raw_text: str, need_brief: dict,
                                cand: dict, readme: str) -> str:
    """采用分支的评估报告（Markdown 文本）。"""
    slim = {k: cand.get(k) for k in ("full_name", "description", "stars", "language",
                                     "topics", "license", "fit_score", "reason")}
    user = (
        f"用户需求：{raw_text}\n需求归纳：{json.dumps(need_brief, ensure_ascii=False)}\n\n"
        f"采用的项目：{json.dumps(slim, ensure_ascii=False)}\n\n"
        f"README（截断）：\n{readme[:12000] or '（未获取到）'}"
    )
    return await llm.chat(SCAFFOLD_ADOPT_SYSTEM, user, max_tokens=2000)


SCAFFOLD_SPLIT_SYSTEM = """你是技术方案拆解器。用户的一句话需求不适合整体采用某个现成框架，需要拆成「技术条目」逐块选型开源组件，输出严格 JSON（不要 markdown 围栏）：
{
  "tech_stack": "主技术栈（Python/Java/JavaScript/TypeScript/Go/Rust/C++ 之一，综合需求与用户技能画像）",
  "items": [
    {"name": "条目名（2-6 字，如：鼠标操作、决策模型、视觉识别、Web 框架、数据存储）",
     "desc": "这个条目负责什么职责、和别的条目怎么配合（60字内）",
     "keywords": ["检索开源项目用的英文关键词，2-4 个（如 pyautogui, mouse automation）"]}
  ]
}
拆解规则：
- 3-8 条，覆盖需求的全部技术面（含前端/交互/数据这类支撑面，不是只有核心算法）
- 条目是「可独立选型开源组件的技术能力」，不是业务功能清单
- 一句话里隐含的支撑能力也要拆出来（如游戏辅助工具离不开「窗口/屏幕捕获」）
- 别太碎：日志、配置这类通用杂项不单独成条"""


async def scaffold_split(llm: LLMClient, raw_text: str, need_brief: dict, profile: str) -> dict:
    """一句话需求 → 技术条目骨架 + 主技术栈推断。"""
    user = (
        f"{PROFILE_NOTE.format(profile=profile)}\n\n用户需求：{raw_text}\n"
        f"需求归纳：{json.dumps(need_brief, ensure_ascii=False)}"
    )
    return await llm.chat_json(SCAFFOLD_SPLIT_SYSTEM, user, max_tokens=2000)


SCAFFOLD_BASE_SYSTEM = """你是脚手架架构判定器。给定用户需求、技术条目与每条的选型结果，判断哪个**已选中的项目**作为 base 主干（其余组件挂载其上），输出严格 JSON（不要 markdown 围栏）：
{"base": "owner/repo（必须是选型结果之一）或空字符串（无可挂载的主干）",
 "rationale": "为什么它适合当主干（80字内）",
 "mounting": "各组件如何挂载的简述（120字内，如：路由与静态托管都在它上面，存储作为服务层，前端由它托管）"}
规则：base 优先选 Web 框架 / 应用骨架类的已选项目；条目全是自研、或选中的都是纯库（无主干可言）时 base 给空字符串，生成时用主语言默认骨架。"""


async def scaffold_base(llm: LLMClient, raw_text: str, tech_stack: str, items: list[dict]) -> dict:
    """选型完成后预判 base 主干与挂载关系（生成 agent 的输入之一）。"""
    slim = [
        {"no": it.get("no"), "name": it.get("name"),
         "selected": it.get("selected") or ("自研" if it.get("self_dev") else "未选"),
         "language": (it.get("candidates") or [{}])[0].get("language", "") if it.get("selected") else ""}
        for it in items
    ]
    user = (f"用户需求：{raw_text}\n主技术栈：{tech_stack or '未定'}\n\n条目与选型：\n"
            + json.dumps(slim, ensure_ascii=False))
    return await llm.chat_json(SCAFFOLD_BASE_SYSTEM, user, max_tokens=800)


# ---------- 教材（books） ----------

BOOK_OUTLINE_SYSTEM = """你是教材结构分析器。根据抽样页文本与 PDF 书签，归纳这本书的学习大纲。
输出严格 JSON（不要 markdown 围栏）：
{"chapters": [{"no": 章序号(从1起), "title": "章标题", "start": 起始页码(整数), "end": 结束页码(整数),
               "summary": "本章讲什么（两三句）", "why": "为什么值得学（一句，结合考证/实战）"}]}
规则：
- 章按页码升序、首尾相接覆盖全书（第 1 章从第 1 页附近开始，最后一章 end 等于总页数）
- 页码只能依据抽样页的实际页码与书签页码推算，不确定的边界宁粗勿漏
- 一般 8-20 章；书签可靠时优先沿用书签的章划分
- 前言/目录/附录不单独成章（并入第 1 章或最后一章）"""


async def book_outline(llm: LLMClient, title: str, pages: int, toc: list[dict], sampled: list[dict]) -> dict:
    """教材学习大纲：章 → 页范围（页码 1 起、含端点）。"""
    user = (
        f"书名：{title or '（未知）'}；总页数：{pages}\n"
        f"PDF 书签（可能残缺或为空）：{json.dumps(toc, ensure_ascii=False) if toc else '无'}\n\n"
        f"抽样页（每页开头片段，page 为全书实际页码）：\n{json.dumps(sampled, ensure_ascii=False)}"
    )
    return await llm.chat_json(BOOK_OUTLINE_SYSTEM, user, max_tokens=8000)
