"""进程内 asyncio 后台任务管理器：提交、进度更新、状态查询。不引入 Celery/Redis。"""
import asyncio
import uuid
from datetime import datetime, timezone

from .db import SessionLocal
from .models import TaskRun

MAX_LOGS = 200  # 时间线封顶，超出丢最早的（前端只看最近的过程）


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sync_update(task_id: str, **fields) -> None:
    with SessionLocal() as s:
        row = s.get(TaskRun, task_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()


def _append_log(task_id: str, msg: str, **extra) -> None:
    """追加一行进度时间线，并同步刷新 progress（沿用旧的「最新一行」语义）。

    JSON 列**原地 append 不触发 SQLAlchemy 的 dirty 检测**——必须整体重新赋值，否则不落库。
    """
    with SessionLocal() as s:
        row = s.get(TaskRun, task_id)
        if row is None:
            return
        entry = {"t": _now().isoformat(timespec="milliseconds"), "msg": msg}
        row.logs = [*(row.logs or []), entry][-MAX_LOGS:]
        row.progress = msg
        for k, v in extra.items():
            setattr(row, k, v)
        s.commit()


def _clear_pending_question(task_id: str) -> None:
    """终态清场兜底：status 无关地 pop payload.pending_question，并幂等注销问题桥。

    工具侧 _restore_running 是第一道清场（AD-1 单写者），但引擎吞工具异常、或任务层
    在问询挂起期间直接写终态时，残留的 pending_question 会让 UI 挂僵尸问题——这里补第二道网。
    askuser 顶置 import 会成环（它已 `from ..tasks import _now`），必须惰性导入；
    惰性 import 也进 try：兜底网在终态写入之后再抛异常会顶掉 _runner 的收尾语义。
    """
    try:
        from .services import askuser  # noqa: PLC0415 惰性导入防环

        try:
            with SessionLocal() as s:
                row = s.get(TaskRun, task_id)
                if row is not None:
                    payload = dict(row.payload or {})
                    if payload.pop("pending_question", None) is not None:
                        row.payload = payload
                        s.commit()
        except Exception:  # noqa: BLE001 清键失败不影响注销
            pass
        askuser.abort_bridge(task_id)  # 无桥静默返回（幂等）
    except Exception:  # noqa: BLE001 兜底网绝不外抛（含 import 失败）
        pass


def _task_view(row: TaskRun, with_logs: bool = False) -> dict:
    view = {
        "id": row.id, "type": row.type, "status": row.status, "progress": row.progress,
        "error": row.error, "payload": row.payload,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
    if with_logs:
        view["logs"] = row.logs or []
    return view


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, type_: str, coro_factory, payload: dict | None = None) -> str:
        """coro_factory: 接收 (task_id, progress_cb, log_cb) 返回协程的工厂。

        progress_cb 只留最新一行（短视频任务够用），log_cb 追加时间线（长任务用）——
        实测 AI 命令任务跑 74~338s，单字段覆盖会让中间过程互相冲掉，界面看着像卡死。
        """
        task_id = uuid.uuid4().hex[:12]
        with SessionLocal() as s:
            s.add(TaskRun(id=task_id, type=type_, payload=payload or {}))
            s.commit()

        async def _runner() -> None:
            def progress(msg: str) -> None:
                _sync_update(task_id, progress=msg)

            def log(msg: str) -> None:
                _append_log(task_id, msg)

            try:
                await coro_factory(task_id, progress, log)
                _append_log(task_id, "完成", status="success", finished_at=_now())
                _clear_pending_question(task_id)
            except asyncio.CancelledError:
                _append_log(task_id, "失败：已取消", status="failed", error="已取消",
                            finished_at=_now())
                _clear_pending_question(task_id)
                raise
            except Exception as e:  # noqa: BLE001 任务层兜底，错误落库展示
                err = f"{type(e).__name__}: {e}"
                _append_log(task_id, f"失败：{err}", status="failed", error=err, finished_at=_now())
                _clear_pending_question(task_id)

        self._tasks[task_id] = asyncio.create_task(_runner(), name=task_id)
        return task_id

    def list(self, limit: int = 20) -> list[dict]:
        with SessionLocal() as s:
            rows = s.query(TaskRun).order_by(TaskRun.created_at.desc()).limit(limit).all()
            return [_task_view(r) for r in rows]

    def get(self, task_id: str) -> dict | None:
        """单个任务（含 logs 时间线）；列表接口不带 logs 省流量，前端恢复时按需补拉。"""
        with SessionLocal() as s:
            row = s.get(TaskRun, task_id)
            return _task_view(row, with_logs=True) if row else None


manager = TaskManager()
