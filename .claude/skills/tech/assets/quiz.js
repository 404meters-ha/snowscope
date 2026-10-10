/*
 * celestial-snow 学习闭环 · 共享 quiz 组件（/tech 生成课程时原样拷入 courses/{id}/assets/）
 *
 * 用法一：随堂检测（每节课程 HTML 尾部）
 *   <div id="quiz"></div>
 *   <script id="quiz-spec" type="application/json">{...}</script>
 *   <script src="assets/quiz.js"></script>
 *   spec 格式：
 *   {
 *     "course_id": 3,                    // 注册课程时平台返回的 id，写死进页面
 *     "lesson_id": "01-overview",        // 与 POST /api/courses 的 lessons[].lesson_id 一致
 *     "title": "随堂检测",
 *     "questions": [
 *       {"type": "single", "question": "…", "options": ["…","…","…","…"], "answer": [2], "explain": "…"},
 *       {"type": "multi",  "question": "…", "options": ["…","…","…"],    "answer": [0,2], "explain": "…"},
 *       {"type": "judge",  "question": "…", "options": ["正确","错误"],   "answer": [0],  "explain": "…"}
 *     ]
 *   }
 *   answer 为正确选项下标数组（单选/判断长度 1，多选 ≥2）。判分在页面即时反馈，提交后回传平台存档。
 *
 * 用法二：课程首页进度（index.html）
 *   <div id="course-progress"></div>
 *   <script>window.COURSE_ID = 3;</script>
 *   <script src="assets/quiz.js"></script>
 */
(function () {
  'use strict';

  // ---------- 工具 ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function chosenIndices(q, root) {
    var name = 'q' + q._i;
    var inputs = root.querySelectorAll('input[name="' + name + '"]:checked');
    var idx = Array.prototype.map.call(inputs, function (el) { return Number(el.value); });
    idx.sort(function (a, b) { return a - b; });
    return idx;
  }

  function sameSet(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  var TYPE_LABEL = { single: '单选', multi: '多选', judge: '判断' };

  // ---------- 随堂检测 ----------
  function Quiz(root, spec) {
    this.root = root;
    this.spec = spec;
    this.total = (spec.questions || []).length;
    this.render();
  }

  Quiz.prototype.render = function () {
    var self = this;
    var html = ['<fieldset class="quiz-box"><legend>' + esc(this.spec.title || '随堂检测') + '</legend>'];
    (this.spec.questions || []).forEach(function (q, i) {
      q._i = i;
      var type = q.type === 'multi' ? 'checkbox' : 'radio';
      html.push('<div class="quiz-q" data-qi="' + i + '">');
      html.push('<div class="quiz-q-title"><span class="quiz-tag">' + (TYPE_LABEL[q.type] || '单选') + '</span>' +
        (i + 1) + '. ' + esc(q.question) + '</div>');
      (q.options || []).forEach(function (opt, oi) {
        html.push('<label class="quiz-opt"><input type="' + type + '" name="q' + i + '" value="' + oi + '"> ' +
          String.fromCharCode(65 + oi) + '. ' + esc(opt) + '</label>');
      });
      html.push('<div class="quiz-fb" hidden></div>');
      html.push('</div>');
    });
    html.push('<div class="quiz-actions"><button type="button" class="quiz-submit">提交测验</button>' +
      '<button type="button" class="quiz-reset" hidden>重做</button>' +
      '<span class="quiz-status" role="status"></span></div>');
    html.push('</fieldset>');
    this.root.innerHTML = html.join('');

    this.root.querySelector('.quiz-submit').addEventListener('click', function () { self.submit(); });
    this.root.querySelector('.quiz-reset').addEventListener('click', function () { self.render(); });
  };

  Quiz.prototype.submit = function () {
    var spec = this.spec;
    var score = 0;
    var detail = [];
    var self = this;
    (spec.questions || []).forEach(function (q) {
      var chosen = chosenIndices(q, self.root);
      var ok = sameSet(chosen, (q.answer || []).slice().sort(function (a, b) { return a - b; }));
      if (ok) score++;
      detail.push({ question: q.question, chosen: chosen, answer: q.answer, correct: ok });
      var qEl = self.root.querySelector('.quiz-q[data-qi="' + q._i + '"]');
      qEl.classList.add(ok ? 'quiz-right' : 'quiz-wrong');
      var fb = qEl.querySelector('.quiz-fb');
      fb.hidden = false;
      var letters = (q.answer || []).map(function (i) { return String.fromCharCode(65 + i); }).join('、');
      fb.innerHTML = (ok ? '✅ 答对了' : '❌ 正确答案：' + letters) +
        (q.explain ? ' — ' + esc(q.explain) : '');
    });

    var status = this.root.querySelector('.quiz-status');
    status.textContent = '正在回传平台…';
    // 相对路径回退两级：课程页固定在 courses/{id}/ 顶层，本地解析为 /api/…，
    // 子路径部署（如 /celestial-snow/）解析为 {BASE}/api/…，两种部署都命中平台
    fetch('../../api/quiz-results', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        course_id: spec.course_id,
        lesson_id: spec.lesson_id,
        score: score,
        total: this.total,
        detail: detail,
      }),
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      status.textContent = '得分 ' + score + '/' + self.total +
        ' · 已回传平台' + (data.course_status === 'done' ? ' · 🎉 全部节完成，该 issue 已标记为已完成！' : '');
      self.root.querySelector('.quiz-submit').disabled = true;
      self.root.querySelector('.quiz-reset').hidden = false;
    }).catch(function () {
      // 发布到服务器目录的静态副本与平台不同源，回传必然失败——那不是故障，如实说明即可
      status.textContent = '得分 ' + score + '/' + self.total + (window.CELESTIAL_PUBLISHED
        ? ' · 这是发布出来的静态副本，成绩不计入平台进度（在平台页面打开课程可正常记录）'
        : ' · ⚠️ 回传失败（平台未启动？），本次成绩未记录，可重做后再提交');
      self.root.querySelector('.quiz-submit').disabled = true;
      self.root.querySelector('.quiz-reset').hidden = false;
    });
  };

  // ---------- 课程首页进度 ----------
  var CourseProgress = {
    render: function (containerId, courseId) {
      var el = document.getElementById(containerId);
      if (!el) return;
      el.innerHTML = '<p class="cp-loading">正在加载学习进度…</p>';
      // 同上：相对路径回退两级，兼容子路径部署（见 quiz 回传处的说明）
      fetch('../../api/courses/' + courseId).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      }).then(function (c) {
        var rows = (c.lessons || []).map(function (l) {
          return '<li class="' + (l.submitted ? 'cp-done' : 'cp-todo') + '">' +
            '<a href="' + esc(l.file) + '">' + esc(l.title) + '</a>' +
            '<span class="cp-state">' + (l.submitted ? '✅ ' + l.score + '/' + l.total : '⬜ 待学习') + '</span></li>';
        }).join('');
        el.innerHTML =
          '<p class="cp-summary">进度：' + c.done_lessons + '/' + c.total_lessons + ' 节 quiz 已提交 · ' +
          (c.status === 'done' ? '🎉 课程已完成' : '📖 学习中') + '</p>' +
          '<ol class="cp-list">' + rows + '</ol>';
      }).catch(function () {
        el.innerHTML = window.CELESTIAL_PUBLISHED
          ? '<p class="cp-loading">这是发布出来的静态副本，学习进度以平台页面为准——可直接点下方课程目录学习</p>'
          : '<p class="cp-loading">进度加载失败（平台未启动？）——可直接点下方课程目录学习</p>';
      });
    },
  };

  // ---------- 自动初始化 ----------
  function autoInit() {
    var holder = document.getElementById('quiz');
    var specEl = document.getElementById('quiz-spec');
    if (holder && specEl) {
      try {
        new Quiz(holder, JSON.parse(specEl.textContent));
      } catch (e) {
        holder.innerHTML = '<p class="quiz-status">quiz 配置解析失败：' + esc(e.message) + '</p>';
      }
    }
    if (window.COURSE_ID != null && document.getElementById('course-progress')) {
      CourseProgress.render('course-progress', window.COURSE_ID);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoInit);
  } else {
    autoInit();
  }

  window.TechQuiz = { quiz: Quiz, courseProgress: CourseProgress };
})();
