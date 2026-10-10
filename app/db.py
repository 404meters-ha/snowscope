"""SQLAlchemy 引擎与会话。"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

engine = create_engine(
    get_settings().db_url,
    # timeout：长任务持写锁时别立刻抛 "database is locked"；前端轮询读也在同一把锁上排队
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL：读不阻塞写，后台任务写日志时前端轮询照常读（会多出 -wal/-shm 文件，正常）。"""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """SQLite 的 create_all 不会改旧表：缺的列在这里手工 ALTER 补齐。"""
    from sqlalchemy import inspect, text

    with engine.connect() as conn:
        columns = {c["name"] for c in inspect(engine).get_columns("issues")}
        if "learning_status" not in columns:
            conn.execute(text("ALTER TABLE issues ADD COLUMN learning_status VARCHAR(16)"))
        if "fixed_hint" not in columns:
            conn.execute(text("ALTER TABLE issues ADD COLUMN fixed_hint BOOLEAN DEFAULT 0"))
        if "state" not in columns:  # 存量行恒为 open（摄入只拉 open），无需回填
            conn.execute(text("ALTER TABLE issues ADD COLUMN state VARCHAR(16) DEFAULT 'open'"))
        if "closed_at" not in columns:
            conn.execute(text("ALTER TABLE issues ADD COLUMN closed_at DATETIME"))
        if "fixed_by_pr" not in columns:
            conn.execute(text("ALTER TABLE issues ADD COLUMN fixed_by_pr INTEGER"))
        repo_columns = {c["name"] for c in inspect(engine).get_columns("repos")}
        if "tags" not in repo_columns:
            conn.execute(text("ALTER TABLE repos ADD COLUMN tags JSON DEFAULT '[]'"))
        if "zh_desc" not in repo_columns:
            conn.execute(text("ALTER TABLE repos ADD COLUMN zh_desc TEXT DEFAULT ''"))
        analysis_columns = {c["name"] for c in inspect(engine).get_columns("analyses")}
        if "report_md" not in analysis_columns:
            conn.execute(text("ALTER TABLE analyses ADD COLUMN report_md TEXT DEFAULT ''"))
        course_columns = {c["name"] for c in inspect(engine).get_columns("courses")}
        if "publish" not in course_columns:
            conn.execute(text("ALTER TABLE courses ADD COLUMN publish JSON DEFAULT '{}'"))
        if "series" not in course_columns:
            conn.execute(text("ALTER TABLE courses ADD COLUMN series VARCHAR(200)"))
        task_columns = {c["name"] for c in inspect(engine).get_columns("task_runs")}
        if "logs" not in task_columns:
            conn.execute(text("ALTER TABLE task_runs ADD COLUMN logs JSON DEFAULT '[]'"))
        conn.commit()

    # 学习模块化：旧 courses 的 issue_id/repo_id 是 NOT NULL，课程还要能来自项目/教材/导入。
    # SQLite 改不了列约束，走「读出 → 重建 → 回填」（表小秒级完成；存量课 source_type 一律 issue）。
    course_columns = {c["name"] for c in inspect(engine).get_columns("courses")}
    if "source_type" not in course_columns:
        _rebuild_courses()

    # fixed_hint 是物化列（分页排序用）：存量行按正则一次性回填
    issue_columns = {c["name"] for c in inspect(engine).get_columns("issues")}
    if "fixed_hint" in issue_columns:
        _backfill_fixed_hint()


def _rebuild_courses() -> None:
    """courses 重建为多来源结构（source_type/book_id、issue_id/repo_id 可空）。

    必须用裸 SQL 读旧行——ORM 的 Course 已带新列，SELECT 会在旧表上直接报缺列。
    lessons/publish 裸读出来就是 JSON 文本，原样写回；book_id 旧表没有，回填 0。
    """
    from sqlalchemy import text

    from .models import Course

    legacy_cols = "id, user_id, repo_id, issue_id, title, lessons, status, publish, created_at"
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(f"SELECT {legacy_cols} FROM courses"))]
    with engine.begin() as conn:
        # 索引随表一起删，避免 create_all 重建同名索引冲突
        conn.execute(text("DROP TABLE courses"))
    Base.metadata.create_all(engine, tables=[Course.__table__])
    with engine.begin() as conn:
        for r in rows:
            conn.execute(text(
                f"INSERT INTO courses ({legacy_cols}, book_id, source_type)"
                " VALUES (:id, :user_id, :repo_id, :issue_id, :title, :lessons, :status,"
                " :publish, :created_at, 0, 'issue')"
            ), r)


def _backfill_fixed_hint() -> None:
    """存量 issue 按当前 title/summary/action/screen_reason 重算 fixed_hint（幂等）。"""
    from sqlalchemy import select

    from .models import Issue
    from .services.scoring import refresh_fixed_hint

    with SessionLocal() as session:
        rows = session.execute(select(Issue)).scalars().all()
        changed = 0
        for row in rows:
            old = row.fixed_hint
            if refresh_fixed_hint(row) != old:
                changed += 1
        if changed:
            session.commit()
