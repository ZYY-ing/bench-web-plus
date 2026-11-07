#!/usr/bin/env bash
set -euo pipefail
BASE="${ASCEND_BENCH_HOME:-/home/ascend_bench/runner}"
PORT="${ASCEND_BENCH_PORT:-18787}"
CONTAINER="${ASCEND_BENCH_CONTAINER:-<容器名>}"
if [ ! -d "$BASE/scripts" ]; then
  echo "ASCEND_BENCH_HOME 目录不存在或不完整: $BASE（请先运行 setup.sh 安装）" >&2
  exit 1
fi
cd "$BASE"
mkdir -p logs tmp cache
export ASCEND_BENCH_HOME="$BASE" HOME="$BASE" TMPDIR="$BASE/tmp" XDG_CACHE_HOME="$BASE/cache" PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$BASE/scripts${PYTHONPATH:+:$PYTHONPATH}"
export ASCEND_BENCH_PROCESS_ROLE=runner
export ASCEND_BENCH_API_ONLY=1
# The Runner tokenizes prompts and calls HTTP APIs; it does not initialize NPU
# devices. Match the AISBench child environment before any Python import.
export TORCH_DEVICE_BACKEND_AUTOLOAD=0
# Runner 与测试请求全部直连本机/内网服务，不走企业代理（否则 504 HIS Proxy Notification）
export http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY=
export no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost

# 探测可达的服务 IP（用于隧道提示；容器 host 网络时 127.0.0.1 即宿主机回环）
HOST_IP="$(python3 -I -S -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80));print(s.getsockname()[0])' 2>/dev/null || hostname -I | awk '{print $1}')"

if [ "${ASCEND_BENCH_MANAGED:-0}" = 1 ]; then
  echo "正在启动 Runner；中心将自动核对身份并接入，无需手工建隧道。"
else
cat <<EOF

================================================================
 Runner 启动中！剩下 3 步（全在浏览器，无需改服务器文件）：
----------------------------------------------------------------
 [你的电脑] 1. 建隧道（本地端口随意，多机时各用不同本地端口避免冲突）:
    ssh -N -o ExitOnForwardFailure=yes -L <本地端口>:127.0.0.1:$PORT <SSH用户>@$HOST_IP
    例: 单机 -L 18787:...；多机则机器1=18787、机器2=18788、机器3=18789
 [你的电脑] 2. 打开 http://127.0.0.1:<本地端口>
    登录 token 见: $BASE/runner-token（仅在可信终端读取）
 [网站内]   3. 「步骤1 连接推理服务」填 服务 IP / 端口 / 模型名 / tokenizer 路径
    → 点「检查连接」→ 步骤2 选数据集 → 步骤3 参数 → 开始测试
----------------------------------------------------------------
 验证:  docker exec $CONTAINER curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/
 日志:  docker exec $CONTAINER tail -f $BASE/logs/runner.log
================================================================
EOF
fi

# All installers/recovery launch this script through docker exec -d. Keep the
# Python service as that exec's main process instead of orphaning a nohup child.
# The caller performs the bounded health/identity checks independently.
printf '%s\n' "$$" > runner.starting.pid
PYTHON_BIN="$(python3 -I -S -c 'import sys; print(sys.executable)')"
exec "$PYTHON_BIN" -I -S scripts/runtime_compat.py "$PYTHON_BIN" -u scripts/runner.py < /dev/null >> logs/runner.log 2>&1
