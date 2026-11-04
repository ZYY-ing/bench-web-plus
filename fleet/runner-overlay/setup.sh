#!/usr/bin/env bash
# 在"vLLM 服务容器所在的宿主机"上执行的一键安装脚本。
# 服务 IP / 端口 / 模型名 / tokenizer 不需要在这里传：
# 安装后在网站的「步骤1 连接推理服务」面板填写，点「检查连接」即保存到 Runner。
# 用法: bash setup.sh <测试容器名> <容器内持久化项目目录> [已有ais_bench绝对路径] [Runner端口,默认18787]
set -euo pipefail
CONTAINER="${1:?用法: bash setup.sh <测试容器名> <容器内持久化项目目录> [已有ais_bench绝对路径] [Runner端口,默认18787]}"
TARGET="${2:?请填写已挂载的持久化工作目录（容器内可见的 Linux 绝对路径）}"
AIS="${3:-$TARGET/bench-venv/bin/ais_bench}"
PORT="${4:-18787}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT>0 && PORT<65536)) || { echo 'Runner 端口范围 1–65535'; exit 1; }
[[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo '容器名不合法'; exit 1; }
for value in "$TARGET" "$AIS"; do
  [[ "$value" =~ ^/[a-zA-Z0-9_./-]+$ && "$value" != / && "$value" != *..* ]] || { echo '请使用不含空格及特殊字符的Linux绝对路径'; exit 1; }
done
command -v docker >/dev/null || { echo '未找到 docker'; exit 1; }

echo "== 1/4 环境检查（容器 $CONTAINER，安装到 $TARGET）"
NETWORK="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER")"
[ "$NETWORK" = host ] || { echo '测试容器需要使用 host 网络，以便宿主机 SSH 转发访问回环 Runner。'; exit 1; }
PYBIN="$(docker exec -w / "$CONTAINER" bash -c 'command -v python3.12 || command -v python3')"
[ -n "$PYBIN" ] || { echo '容器内没有 python3'; exit 1; }
docker inspect "$CONTAINER" | python3 -c 'import json,sys; c=json.load(sys.stdin)[0]; t=sys.argv[1]; assert any(m.get("RW") and (t==m["Destination"] or t.startswith(m["Destination"].rstrip("/")+"/")) for m in c["Mounts"]), "工作目录没有持久化RW挂载，停止安装"' "$TARGET"
docker exec -w / "$CONTAINER" "$PYBIN" -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$PORT" || { echo "容器内 $PORT 端口已被占用（可能有其他 Runner 或服务）。可停掉旧 Runner 或换端口重装。"; exit 1; }

echo "== 2/4 拷贝网站、Runner 与数据集"
docker exec -w / "$CONTAINER" mkdir -p "$TARGET"/scripts "$TARGET"/datasets "$TARGET"/logs "$TARGET"/results "$TARGET"/cache "$TARGET"/tmp "$TARGET"/dist
docker exec -w / "$CONTAINER" bash -c 'if [ -f "$1/scripts/runner.py" ]; then mkdir -p "$1/backups"; tar czf "$1/backups/before-setup-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$1" scripts website; fi' _ "$TARGET"
docker cp "$HERE/website" "$CONTAINER:$TARGET/"
docker cp "$HERE/scripts/." "$CONTAINER:$TARGET/scripts/"
docker cp "$HERE/datasets/." "$CONTAINER:$TARGET/datasets/"

echo "== 3/4 准备 AISBench 环境"
if [ -z "${3:-}" ]; then
  echo "   · 拷贝 AISBench 源码（约 70MB）..."
  docker cp "$HERE/benchmark" "$CONTAINER:$TARGET/"
  PIP_INDEX="${ASCEND_BENCH_PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple}"
  PIP_TRUSTED=""
  case "$PIP_INDEX" in http://*) PIP_TRUSTED="trusted-host = ${PIP_INDEX#http://}"; PIP_TRUSTED="${PIP_TRUSTED%%/*}";; esac
  PIP_PROXY_LINE=""
  [ -n "${ASCEND_BENCH_PROXY:-}" ] && PIP_PROXY_LINE="proxy = ${ASCEND_BENCH_PROXY}"
  docker exec -w / "$CONTAINER" bash -c 'printf "[global]\nindex-url = %s\n%s\n%s\ntimeout = 30\nprogress-bar = on\n" "$1" "$2" "$3" > "$4"' _ "$PIP_INDEX" "$PIP_TRUSTED" "$PIP_PROXY_LINE" "$TARGET/pip.conf"
  # 可选：容器 DNS 解析不了内网源域名时，写入 /etc/hosts（格式 "IP 域名"）
  if [ -n "${ASCEND_BENCH_EXTRA_HOSTS:-}" ]; then
    EH_IP="${ASCEND_BENCH_EXTRA_HOSTS%% *}"
    EH_NAME="${ASCEND_BENCH_EXTRA_HOSTS#* }"
    docker exec -w / "$CONTAINER" bash -c 'grep -qE "[[:space:]]$2([[:space:]]|$)" /etc/hosts || echo "$1 $2" >> /etc/hosts' _ "$EH_IP" "$EH_NAME"
    echo "   · 已写入容器 hosts: $EH_IP $EH_NAME"
  fi
  # venv 不存在才创建（存在则复用，不重建）
  if docker exec -w / "$CONTAINER" test -x "$TARGET/bench-venv/bin/python" 2>/dev/null; then
    echo "   · 复用已有虚拟环境（$TARGET/bench-venv）"
  else
    echo "   · 创建虚拟环境..."
    docker exec -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "$PYBIN" -m venv --system-site-packages "$TARGET/bench-venv"
  fi
  # 每次都跑 pip 校验/补齐依赖（幂等：已满足的秒过，缺失的自动补上，避免旧装缺包）
  echo "   · 校验/补齐 AISBench 依赖（幂等，已装则秒过）..."
  ok=0
  for attempt in 1 2 3; do
    echo "   — pip 安装尝试 $attempt/3 —"
    if docker exec -w "$TARGET/benchmark" -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "$TARGET/bench-venv/bin/python" -m pip install --timeout 30 --retries 5 'setuptools<=80.10.2' wheel setuptools_scm 'nltk==3.8' \
       && docker exec -w "$TARGET/benchmark" -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "$TARGET/bench-venv/bin/python" -m pip install --timeout 30 --retries 5 --no-build-isolation "opencv-python-headless==4.11.0.86" -e . -r requirements/api.txt; then
      ok=1; break
    fi
    echo "   pip 失败，5 秒后重试"; sleep 5
  done
  [ "$ok" = 1 ] || { echo 'pip 安装三次失败，请检查容器网络（需可达 mirrors.aliyun.com）'; exit 1; }
  echo "   ✓ AISBench 环境就绪"
  # sitecustomize 补丁装进 venv（请求超时 + 报错详情打进日志）
  _SP="$(docker exec -w / "$CONTAINER" bash -c "ls -d $TARGET/bench-venv/lib/python*/site-packages | head -1")"
  docker exec -w / "$CONTAINER" cp "$TARGET/scripts/sitecustomize.py" "$_SP/sitecustomize.py"
fi
docker exec -w / "$CONTAINER" test -x "$AIS" || { echo "ais_bench 不存在或不可执行: $AIS"; exit 1; }

echo "== 4/4 写初始配置并启动 Runner"
HOST_IP="$(docker exec -w / "$CONTAINER" "$PYBIN" -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80));print(s.getsockname()[0])' 2>/dev/null || hostname -I | awk '{print $1}')"
docker exec -w / "$CONTAINER" "$PYBIN" -c 'import json,sys; from pathlib import Path; t,tok,model,host,port,ais,container=sys.argv[1:]; cfg=dict(service=dict(host=host,port=int(port),model=model,tokenizer_path=tok),ais_bench_path=ais,gsm8k_path=t+"/datasets/gsm8k",container=container); p=Path(t+"/runner-config.json"); p.exists() or p.write_text(json.dumps(cfg,indent=2))' "$TARGET" "" "" "$HOST_IP" "8000" "$AIS" "$CONTAINER"
docker exec -d -e ASCEND_BENCH_MANAGED="${ASCEND_BENCH_MANAGED:-0}" -e ASCEND_BENCH_HOME="$TARGET" -e ASCEND_BENCH_CONTAINER="$CONTAINER" -e ASCEND_BENCH_PORT="$PORT" "$CONTAINER" bash "$TARGET/scripts/start-runner.sh"
sleep 2
docker exec -w / "$CONTAINER" curl --fail -s --max-time 5 "http://127.0.0.1:$PORT/" >/dev/null || { echo 'Runner 未就绪，查看日志: docker exec '"$CONTAINER"' tail -20 '"$TARGET"'/logs/runner.log'; exit 1; }

if [ "${ASCEND_BENCH_MANAGED:-0}" = 1 ]; then
  echo "Runner 安装和启动完成，等待中心核对身份与健康状态。"
  echo "接下来：用户评测入口 → 刷新状态 → 选择 Runner → 配置推理服务 → 开始测试。"
  echo "中心自动维护 SSH 连接，无需手工建隧道或输入 Runner token。"
  echo "结果归档: $TARGET/results/jobs/"
else
cat <<EOF

================================================================
 安装完成！剩下 3 步（全在浏览器，无需改服务器文件）：
----------------------------------------------------------------
 [你的电脑] 1. 建隧道（本地端口随意，多机时各用不同本地端口避免冲突）:
    ssh -N -o ExitOnForwardFailure=yes -L <本地端口>:127.0.0.1:$PORT <SSH用户>@$HOST_IP
    例: 单机 -L 18787:...；多机则机器1=18787、机器2=18788、机器3=18789
 [你的电脑] 2. 打开 http://127.0.0.1:<本地端口>
    登录 token 见: $TARGET/runner-token（仅在可信终端读取）
 [网站内]   3. 「步骤1 连接推理服务」填 服务 IP / 端口 / 模型名 / tokenizer 路径
    → 点「检查连接」→ 步骤2 选数据集 → 步骤3 参数 → 开始真实测试
----------------------------------------------------------------
 服务信息全部在网站上填写并保存到 Runner，重新检查即可切换目标。
 结果归档: $TARGET/results/jobs/   日志: docker exec $CONTAINER tail -f $TARGET/logs/runner.log
================================================================
EOF

fi
