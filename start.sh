#!/usr/bin/env bash
# celestial-snow 服务器一键启动：首次运行交互录入 GitHub token 与 GLM key（写入 .env 持久化），之后直接拉起服务。
# 用法：
#   ./start.sh            # .env 缺配置时才询问，已有配置直接启动
#   ./start.sh --config   # 强制重新录入 token / key
# 可用环境变量覆盖：HOST（默认 0.0.0.0）、PORT（默认 8100）
set -euo pipefail
cd "$(dirname "$0")"

FORCE_CONFIG=0
[[ "${1:-}" == "--config" ]] && FORCE_CONFIG=1

# ---------- Python 环境 ----------
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if [[ ! -x .venv/bin/python ]]; then
    echo ">> 创建虚拟环境 .venv ..."
    if ! "$PY" -m venv .venv 2>/dev/null; then
        echo "!! venv 创建失败：Linux 上先执行 sudo apt install python3-venv" >&2
        exit 1
    fi
fi
echo ">> 检查依赖（首次安装较慢）..."
.venv/bin/python -m pip install -q -r requirements.txt

# ---------- 从 .env 读现有值（仅用于展示掩码，不回显明文）----------
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }
mask() {
    local s=$1
    if [[ -z $s ]]; then echo "未配置"; else echo "${s:0:6}...${s: -4}"; fi
}
GH_EXISTING=$(env_get GITHUB_TOKEN)
LLM_EXISTING=$(env_get LLM_API_KEY)

# ---------- 交互录入 ----------
GH_INPUT=""; LLM_INPUT=""
if (( FORCE_CONFIG )) || [[ -z $GH_EXISTING || -z $LLM_EXISTING ]]; then
    echo "== 密钥配置（输入不回显；直接回车 = 保留现有值）=="
    if (( FORCE_CONFIG )) || [[ -z $GH_EXISTING ]]; then
        read -rs -p "GitHub token [$(mask "$GH_EXISTING")]: " GH_INPUT; echo
    fi
    if (( FORCE_CONFIG )) || [[ -z $LLM_EXISTING ]]; then
        read -rs -p "GLM API key  [$(mask "$LLM_EXISTING")]: " LLM_INPUT; echo
    fi
fi

# ---------- 写回 .env（python 处理，避免 sed 转义问题）----------
if [[ -n $GH_INPUT || -n $LLM_INPUT ]]; then
    GITHUB_TOKEN_IN="$GH_INPUT" LLM_KEY_IN="$LLM_INPUT" .venv/bin/python - <<'PYEOF'
import os, re, pathlib

updates = {}
if os.environ.get("GITHUB_TOKEN_IN"):
    updates["GITHUB_TOKEN"] = os.environ["GITHUB_TOKEN_IN"]
if os.environ.get("LLM_KEY_IN"):
    updates["LLM_API_KEY"] = os.environ["LLM_KEY_IN"]

p = pathlib.Path(".env")
example = pathlib.Path(".env.example")
text = p.read_text(encoding="utf-8") if p.exists() else (
    example.read_text(encoding="utf-8") if example.exists() else "")

def set_kv(text, key, value):
    # 更新已有行，没有则追加
    if re.search(rf"(?m)^{key}=.*$", text):
        return re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", text)
    return text.rstrip("\n") + f"\n{key}={value}\n"

for key, value in updates.items():
    text = set_kv(text, key, value)

# GLM key 已就位但端点为空：补 GLM Coding Plan 默认端点
if updates.get("LLM_API_KEY") and not re.search(r"(?m)^LLM_BASE_URL=\S", text):
    text = set_kv(text, "LLM_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4")

p.write_text(text, encoding="utf-8")
PYEOF
    echo ">> .env 已更新"
fi

# ---------- 启动前校验 ----------
GH_EXISTING=$(env_get GITHUB_TOKEN)
LLM_EXISTING=$(env_get LLM_API_KEY)
if [[ -z $GH_EXISTING || -z $LLM_EXISTING ]]; then
    echo "!! GITHUB_TOKEN / LLM_API_KEY 仍为空，服务无法正常抓取与精析。" >&2
    echo "   重新运行 ./start.sh --config 录入。" >&2
    exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8100}"
echo ">> 启动 celestial-snow：http://$HOST:$PORT （健康检查 GET /api/health）"
exec .venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT"
