// 跟随 vite base：根路径部署为 ''（同源根），子路径部署为 '/celestial-snow' 这类前缀
export const BASE = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '')

async function apiGet(path) {
  const res = await fetch(BASE + path)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status}: ${text.slice(0, 200)}`)
  }
  return res.json()
}

async function apiPost(path, body) {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || `${res.status} 请求失败`)
  }
  return data
}

async function apiDelete(path) {
  const res = await fetch(BASE + path, { method: 'DELETE' })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || `${res.status} 请求失败`)
  }
  return data
}

// 文件上传（multipart）：不能预设 Content-Type，浏览器要自动带 boundary
async function apiUpload(path, form) {
  const res = await fetch(BASE + path, { method: 'POST', body: form })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || `${res.status} 上传失败`)
  }
  return data
}

// 项目榜：分页（limit/offset）+ 精析状态（analyzed=all|done|todo）+ 榜单期次（period）
// + 入库批次周（week：latest=最新批次 / YYYY-Www）+ 标签（tag）
export const getRepos = (params = {}) => {
  const qs = new URLSearchParams()
  if (params.sort) qs.set('sort', params.sort)
  if (params.q) qs.set('q', params.q)
  if (params.limit) qs.set('limit', params.limit)
  if (params.offset) qs.set('offset', params.offset)
  if (params.analyzed && params.analyzed !== 'all') qs.set('analyzed', params.analyzed)
  if (params.period) qs.set('period', params.period)
  if (params.week) qs.set('week', params.week)
  if (Array.isArray(params.tag)) params.tag.forEach((t) => qs.append('tag', t))
  else if (params.tag) qs.set('tag', params.tag)
  return apiGet(`/api/repos?${qs}`)
}
// 入库批次清单（按周聚合 first_seen_at）：批次下拉的选项源
export const getRepoWeeks = () => apiGet('/api/repos/weeks')
export const getRepo = (fullName) => apiGet(`/api/repos/${fullName}`)
export const getReport = (id) => apiGet(`/api/reports/${id}`)
export const getTasks = (limit = 10) => apiGet(`/api/tasks?limit=${limit}`)
// 单个任务（含 logs 时间线）：列表接口不带 logs，进度面板与刷新恢复用这个
export const getTask = (id) => apiGet(`/api/tasks/${id}`)
// 交互式任务应答（waiting 态）：{id: 问题id, answers:[{question,answer}]} 或 {id, cancel:true}
export const answerTask = (id, body) => apiPost(`/api/tasks/${id}/answer`, body)
export const getConfig = () => apiGet('/api/config')
export const getIssues = (params = {}) => {
  const qs = new URLSearchParams()
  if (params.sort) qs.set('sort', params.sort)
  if (params.difficulty) qs.set('difficulty', params.difficulty)
  if (params.repo) qs.set('repo', params.repo)
  if (params.deep_only) qs.set('deep_only', 'true')
  if (params.limit) qs.set('limit', params.limit)
  if (params.offset) qs.set('offset', params.offset)
  if (params.analyzed && params.analyzed !== 'all') qs.set('analyzed', params.analyzed)
  return apiGet(`/api/issues?${qs}`)
}
// Issue 榜筛选器：有 issue 的项目清单（含条数）
export const getIssueRepos = () => apiGet('/api/issue-repos')
export const postRefresh = () => apiPost('/api/refresh')
export const postContribute = (repoIds) => apiPost('/api/contributions', { repo_ids: repoIds })
// 批量精析选中的项目（「未精析」队列入口）
export const postAnalyze = (repoIds) => apiPost('/api/repos/analyze', { repo_ids: repoIds })
// 为缺失中文简介的项目批量生成（英文描述翻译/提炼）
export const postTranslate = () => apiPost('/api/repos/translate')
// 标签 / 定向行业分析
export const getTags = () => apiGet('/api/tags')
export const postAutoTag = () => apiPost('/api/tags/auto')
export const getIndustries = () => apiGet('/api/industries')
export const getIndustry = (id) => apiGet(`/api/industries/${id}`)
// 两段式：先解析（确认面板数据源），确认后跑多方向分析
export const postIndustryParse = (text) => apiPost('/api/industries/parse', { text })
export const postIndustryRuns = (payload) => apiPost('/api/industries', payload)
export const deleteIndustry = (id) => apiDelete(`/api/industries/${id}`)
// 学习闭环：/tech 取上下文、注册课程；课程 HTML 静态托管在 /courses/{id}/
export const getLearningContext = (issueId) => apiGet(`/api/learning-context/${issueId}`)
export const postCourse = (payload) => apiPost('/api/courses', payload)
export const getCourses = () => apiGet('/api/courses')
export const getCourse = (id) => apiGet(`/api/courses/${id}`)
// 删除课件小节（多选批量）：清单/HTML/quiz 存档一并清，删空则整门课连目录删除
export const deleteLessons = (courseId, lessonIds) =>
  apiPost(`/api/courses/${courseId}/lessons/delete`, { lesson_ids: lessonIds })
export const postQuizResult = (payload) => apiPost('/api/quiz-results', payload)
// 教材（电子书 PDF）：上传解析（后台任务）→ 大纲就绪后 /tech-book 生成教程；章节系列走批量任务
export const uploadBook = (file, title = '') => {
  const form = new FormData()
  form.append('file', file)
  form.append('title', title)
  return apiUpload('/api/books', form)
}
export const getBooks = () => apiGet('/api/books')
export const getBook = (id) => apiGet(`/api/books/${id}`)
export const deleteBook = (id) => apiDelete(`/api/books/${id}`)
export const createBookSeries = (id, chapterNos) =>
  apiPost(`/api/books/${id}/series`, { chapter_nos: chapterNos })
// 导入已生成好的教程包（zip）：解压注册进课程列表，旧 course_id 由平台改写；
// series 可空——填了归入该系列，同系列课在列表里聚拢排序
export const importCourse = (file, title = '', series = '') => {
  const form = new FormData()
  form.append('file', file)
  form.append('title', title)
  form.append('series', series)
  return apiUpload('/api/courses/import', form)
}
// 通用技能调用：内置 Agent SDK 执行（SKILL.md 正文注入 prompt），技能文件现读即时生效
export const getSkills = () => apiGet('/api/skills')
export const invokeSkill = (name, args = '') => apiPost(`/api/skills/${name}/invoke`, { args })
// 页面 AI 命令栏：自由指令（/开头即技能）
export const runAgent = (prompt) => apiPost('/api/agent/run', { prompt })
// 脚手架：一句话需求 → 框架匹配（适配度%）→（采用 / 拆条选型 / 生成 zip，分期上线）
export const createScaffoldRequest = (text) => apiPost('/api/scaffold/requests', { text })
export const getScaffolds = (params = {}) => {
  const qs = new URLSearchParams()
  if (params.limit) qs.set('limit', params.limit)
  if (params.offset) qs.set('offset', params.offset)
  return apiGet(`/api/scaffold/requests?${qs}`)
}
export const getScaffold = (id) => apiGet(`/api/scaffold/requests/${id}`)
export const rematchScaffold = (id, text = '') => apiPost(`/api/scaffold/requests/${id}/match`, { text })
export const adoptScaffold = (id, fullName) =>
  apiPost(`/api/scaffold/requests/${id}/adopt`, { full_name: fullName })
// 拆条（同步 2-10s，缓存命中秒回）；条目确认 → 条目级选型任务
async function apiPut(path, body) {
  const res = await fetch(BASE + path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `${res.status} 请求失败`)
  return data
}
export const splitScaffold = (id) => apiPost(`/api/scaffold/requests/${id}/split`)
export const confirmScaffoldItems = (id, items, techStack) =>
  apiPut(`/api/scaffold/requests/${id}/items`, { items, tech_stack: techStack })
// 逐条选型提交（自研条目 full_name 传 null）→ scaffold_build 生成任务
export const submitScaffoldSelection = (id, selections) =>
  apiPost(`/api/scaffold/requests/${id}/select`, { selections })
