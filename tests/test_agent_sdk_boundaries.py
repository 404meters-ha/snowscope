"""agent_sdk 文件工具白名单边界测试（纯断言脚本，无 pytest 依赖）。

用法：项目根执行 .venv/Scripts/python.exe tests/test_agent_sdk_boundaries.py
覆盖：scaffolds/ 写白名单新增、旧 courses/ 能力不回归、穿越/前缀伪造/越权全拒。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.services.agent_sdk import (  # noqa: E402
    PROJECT_ROOT, ListDirTool, ReadFileTool, WriteFileTool,
)

PASS, FAIL = [], []


def check(name: str, ok: bool):
    (PASS if ok else FAIL).append(name)
    print(f"{'✓' if ok else '✗'} {name}")


def is_refused(result) -> bool:
    return getattr(result, "is_error", False) and "拒绝" in str(getattr(result, "content", ""))


def main() -> None:
    w, r, d = WriteFileTool(), ReadFileTool(), ListDirTool()

    # ── 写白名单：新增 scaffolds/ 能写 ──
    res = w.execute(path="scaffolds/workspace/dryrun/_boundary_probe.py", content="x = 1\n")
    check("写 scaffolds/workspace/ 放行", not getattr(res, "is_error", False))
    res = w.execute(path=str(PROJECT_ROOT / "scaffolds" / "ws2" / "abs.py"), content="x = 1\n")
    check("写 scaffolds/ 绝对路径放行", not getattr(res, "is_error", False))

    # ── 写白名单：旧能力不回归 ──
    res = w.execute(path="courses/_boundary_probe.txt", content="probe\n")
    check("写 courses/ 放行（不回归）", not getattr(res, "is_error", False))

    # ── 越权全拒 ──
    check("写 app/api.py 拒绝", is_refused(w.execute(path="app/api.py", content="hack")))
    check("写 .env 拒绝", is_refused(w.execute(path=".env", content="LEAK=1")))
    check("写 main.py（项目根）拒绝", is_refused(w.execute(path="main.py", content="hack")))
    check("写 celestial.db 拒绝", is_refused(w.execute(path="celestial.db", content="x")))
    check("写 scaffolds/../app/api.py 穿越 拒绝",
          is_refused(w.execute(path="scaffolds/../app/api.py", content="hack")))
    check("写 scaffolds/../../.env 穿越 拒绝",
          is_refused(w.execute(path="scaffolds/../../.env", content="LEAK=1")))
    check("绝对路径穿越 .../scaffolds/../../app 拒绝",
          is_refused(w.execute(path=str(PROJECT_ROOT / "scaffolds" / ".." / "app" / "api.py"),
                               content="hack")))
    check("前缀伪造 scaffolds-evil/ 拒绝",
          is_refused(w.execute(path="scaffolds-evil/x.py", content="hack")))
    check("写项目外临时目录 拒绝",
          is_refused(w.execute(path=str(Path.home() / "evil.py"), content="hack")))

    # ── 读/列白名单 ──
    res = r.execute(path="scaffolds/workspace/dryrun/_boundary_probe.py")
    check("读 scaffolds/ 放行", not is_refused(res))  # 文件刚写过，存在
    check("读 .env 拒绝", is_refused(r.execute(path=".env")))
    check("读 app/config.py 拒绝", is_refused(r.execute(path="app/config.py")))
    check("列 scaffolds/ 放行", not getattr(d.execute(path="scaffolds"), "is_error", False))
    check("列项目根 拒绝", is_refused(d.execute(path=".")))

    # ── 清理探针 ──
    for probe in [PROJECT_ROOT / "scaffolds" / "workspace" / "dryrun" / "_boundary_probe.py",
                  PROJECT_ROOT / "scaffolds" / "ws2" / "abs.py",
                  PROJECT_ROOT / "courses" / "_boundary_probe.txt"]:
        probe.unlink(missing_ok=True)
    (PROJECT_ROOT / "scaffolds" / "ws2").rmdir()

    print(f"\n{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
