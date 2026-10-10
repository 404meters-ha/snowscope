"""引擎截断恢复直跑断言（纯断言脚本，无 pytest 依赖、无网络、不动 DB）。

用法：项目根执行 .venv/Scripts/python.exe tests/test_truncation_recovery.py
背景：scaffold 生成 E2E 实测一条 16k 截断响应直接终结整个 turn（六件套全缺），
engine 需要把截断当可恢复状态而不是仅告警。覆盖四态：
① 纯文本截断 → 注入「继续」提示续跑，下一轮正常收尾；
② 连续截断封顶（_MAX_TRUNCATION_CONTINUATIONS）后照旧收尾，不死循环；
③ 截断响应里参数损坏（input={}）的工具调用弃执行、错误 tool_result 兜底（协议对齐）；
④ 截断响应里完好调用照常执行，与损坏调用的结果合入同一条回传消息；
⑤ 回归：end_turn 正常路径单次调用、无 error 事件。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from core.engine import Engine, _MAX_TRUNCATION_CONTINUATIONS  # noqa: E402
from core.llm import LLMMessage, LLMUsage  # noqa: E402
from core.permissions import PermissionChecker  # noqa: E402
from core.tool import Tool, ToolResult  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool):
    (PASS if ok else FAIL).append(name)
    print(f"{'✓' if ok else '✗'} {name}")


class SpyTool(Tool):
    """执行即置位的探针工具：断言「被弃执行」用。"""

    name = "Echo"
    description = "spy"
    input_schema = {"type": "object", "properties": {"message": {"string": "string"}},
                    "required": ["message"]}

    def __init__(self):
        self.executed = 0

    def execute(self, message: str = "", **_) -> ToolResult:
        self.executed += 1
        return ToolResult(content=f"Echo: {message}")


def _make_engine():
    tool = SpyTool()
    # 与 agent_sdk 生产构造同形（provider=openai）：本 venv 的 anthropic SDK 用 httpx2，
    # 默认 provider 走 httpx.Timeout 会构造失败——平台运行时本就只走 openai 路径
    return Engine(tools=[tool], system_prompt="test",
                  permission_checker=PermissionChecker(auto_approve=True),
                  provider="openai", api_key="test",
                  base_url="http://127.0.0.1:1"), tool


def _stream(final_msg: LLMMessage, chunks: list[str]):
    s = MagicMock()
    s.__enter__ = MagicMock(return_value=s)
    s.__exit__ = MagicMock(return_value=False)
    s.text_stream = iter(chunks)
    s.get_final_message = MagicMock(return_value=final_msg)
    return s


def _text_msg(text: str, stop: str) -> LLMMessage:
    return LLMMessage(content=[{"type": "text", "text": text}],
                      usage=LLMUsage(), stop_reason=stop)


def _tool_msg(blocks: list[dict], stop: str = "max_tokens") -> LLMMessage:
    return LLMMessage(content=blocks, usage=LLMUsage(), stop_reason=stop)


def scenario_pure_text_truncation_continues() -> None:
    """① 纯文本截断 → nudge 续跑 → 第二轮完成。"""
    engine, _ = _make_engine()
    streams = [_stream(_text_msg("半截的计划…", "max_tokens"), ["半截的计划…"]),
               _stream(_text_msg("计划完成", "end_turn"), ["计划完成"])]
    with patch.object(engine._client, "stream_messages", side_effect=streams) as sm:
        events = list(engine.submit("生成骨架"))

    check("① 纯文本截断: 触发两次 API 调用（续跑生效）", sm.call_count == 2)
    nudge = sm.call_args_list[1].kwargs["messages"][2]
    check("① 纯文本截断: nudge 是 user 消息且言明截断",
          nudge["role"] == "user" and "截断" in str(nudge["content"]))
    errs = [e[1] for e in events if e[0] == "error"]
    check("① 纯文本截断: 时间线带截断告警与续跑注记",
          any("truncated" in e.lower() for e in errs)
          and any("continuation" in e for e in errs))
    check("① 纯文本截断: 第二轮文本事件到达",
          any(e[0] == "text" and "计划完成" in e[1] for e in events))


def scenario_consecutive_truncation_caps() -> None:
    """② 连续截断封顶后收尾，不死循环。"""
    engine, _ = _make_engine()
    cap = _MAX_TRUNCATION_CONTINUATIONS
    streams = [_stream(_text_msg(f"截断第{i}段", "max_tokens"), [f"截断第{i}段"])
               for i in range(cap + 1)]  # 多备一个：若失控会打穿 side_effect 抛 StopIteration
    with patch.object(engine._client, "stream_messages", side_effect=streams) as sm:
        events = list(engine.submit("生成骨架"))
    check(f"② 连续截断: 恰好封顶 {cap} 次调用后收尾", sm.call_count == cap)
    check("② 连续截断: 正常走完不抛异常", all(e[0] != "abort" for e in events))


def scenario_damaged_tool_call_not_executed() -> None:
    """③ 截断损坏（input={}）的调用弃执行，错误 tool_result 兜底。"""
    engine, tool = _make_engine()
    streams = [_stream(_tool_msg([{"type": "tool_use", "id": "tu_1",
                                   "name": "Echo", "input": {}}]), []),
               _stream(_text_msg("重发完成", "end_turn"), ["重发完成"])]
    with patch.object(engine._client, "stream_messages", side_effect=streams) as sm:
        events = list(engine.submit("echo"))
    results = [e for e in events if e[0] == "tool_result"]
    check("③ 损坏调用: 未执行（探针零次）", tool.executed == 0)
    check("③ 损坏调用: 有错误 tool_result 且言明截断",
          len(results) == 1 and results[0][3].is_error and "截断" in results[0][3].content)
    second_msgs = sm.call_args_list[1].kwargs["messages"]
    tr = [b for b in second_msgs[2]["content"] if b.get("type") == "tool_result"]
    check("③ 损坏调用: 回传消息 tool_result 与 tool_use_id 对齐（协议完整）",
          len(tr) == 1 and tr[0]["tool_use_id"] == "tu_1" and tr[0]["is_error"])


def scenario_intact_call_survives_truncation() -> None:
    """④ 截断响应中完好调用照常执行，结果与损坏调用合入同一条回传。"""
    engine, tool = _make_engine()
    streams = [_stream(_tool_msg([
        {"type": "tool_use", "id": "tu_ok", "name": "Echo", "input": {"message": "hi"}},
        {"type": "tool_use", "id": "tu_bad", "name": "Echo", "input": {}},
    ]), []),
        _stream(_text_msg("done", "end_turn"), ["done"])]
    with patch.object(engine._client, "stream_messages", side_effect=streams) as sm:
        events = list(engine.submit("echo twice"))
    check("④ 混合截断: 完好调用执行了一次", tool.executed == 1)
    results = {e[2].get("message") if e[2] else "": e[3] for e in events if e[0] == "tool_result"}
    ok_r = [e for e in events if e[0] == "tool_result" and not e[3].is_error]
    bad_r = [e for e in events if e[0] == "tool_result" and e[3].is_error]
    check("④ 混合截断: 一条成功一条截断错误", len(ok_r) == 1 and len(bad_r) == 1)
    second_msgs = sm.call_args_list[1].kwargs["messages"]
    trs = [b for b in second_msgs[2]["content"] if b.get("type") == "tool_result"]
    ids = {t["tool_use_id"] for t in trs}
    check("④ 混合截断: 两个 tool_use 都有兜底结果", ids == {"tu_ok", "tu_bad"} and len(trs) == 2)


def scenario_end_turn_untouched() -> None:
    """⑤ 回归：正常 end_turn 单次调用、无 error 事件。"""
    engine, tool = _make_engine()
    with patch.object(engine._client, "stream_messages",
                      return_value=_stream(_text_msg("ok", "end_turn"), ["ok"])) as sm:
        events = list(engine.submit("hi"))
    check("⑤ 正常路径: 单次调用、无 error、无多余消息",
          sm.call_count == 1 and not [e for e in events if e[0] == "error"]
          and len(engine.get_messages()) == 2)


def scenario_scaffold_cap_raised() -> None:
    """⑥ scaffold 单轮输出上限已升到 32768（16k 实测被连写多文件顶满）。"""
    import re
    src = (ROOT / "app" / "services" / "scaffold_builder.py").read_text(encoding="utf-8")
    m = re.search(r"BUILD_MAX_TOKENS\s*=\s*(\d+)", src)
    check("⑥ scaffold: BUILD_MAX_TOKENS = 32768", m is not None and m.group(1) == "32768")


def main() -> None:
    scenario_pure_text_truncation_continues()
    scenario_consecutive_truncation_caps()
    scenario_damaged_tool_call_not_executed()
    scenario_intact_call_survives_truncation()
    scenario_end_turn_untouched()
    scenario_scaffold_cap_raised()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
