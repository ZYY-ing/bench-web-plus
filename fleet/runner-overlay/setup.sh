#!/usr/bin/env bash
# 在"vLLM 服务容器所在的宿主机"上执行的一键安装脚本。
# 服务 IP / 端口 / 模型名 / tokenizer 不需要在这里传：
# 安装后在网站的「步骤1 连接推理服务」面板填写，点「检查连接」即保存到 Runner。
# 用法: bash setup.sh <测试容器名> <容器内持久化项目目录> [已有ais_bench绝对路径] [Runner端口,默认18787]
set -euo pipefail
CONTAINER="${1:?用法: bash setup.sh <测试容器名> <容器内持久化项目目录> [已有ais_bench绝对路径] [Runner端口,默认18787]}"
TARGET="${2:?请填写已挂载的持久化工作目录（容器内可见的 Linux 绝对路径）}"
AIS="${3:-}"
PORT="${4:-18787}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT>0 && PORT<65536)) || { echo 'Runner 端口范围 1–65535'; exit 1; }
[[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || { echo '容器名不合法'; exit 1; }
for value in "$TARGET" "$AIS"; do
  [ -z "$value" ] && continue
  [[ "$value" =~ ^/[a-zA-Z0-9_./-]+$ && "$value" != / && "$value" != *..* ]] || { echo '请使用不含空格及特殊字符的Linux绝对路径'; exit 1; }
done
command -v docker >/dev/null || { echo '未找到 docker'; exit 1; }
CONTAINER_ID="$(docker inspect --format '{{.Id}}' "$CONTAINER")"
[[ "$CONTAINER_ID" =~ ^[a-f0-9]{64}$ ]] || { echo '容器身份无效'; exit 1; }
VENV="$TARGET/environments/$CONTAINER_ID/bench-venv"
BENCHMARK="$TARGET/environments/$CONTAINER_ID/benchmark"
AIS="${AIS:-$VENV/bin/ais_bench}"

echo "== 1/4 环境检查（容器 $CONTAINER，安装到 $TARGET）"
NETWORK="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER")"
[ "$NETWORK" = host ] || { echo '测试容器需要使用 host 网络，以便宿主机 SSH 转发访问回环 Runner。'; exit 1; }
NEEDS_VENV=0
[ -z "${3:-}" ] && NEEDS_VENV=1
PYBIN="$(docker exec -i -w / "$CONTAINER" python3 -I -S - "$NEEDS_VENV" <<'PYTHON_SELECT'
import os,shutil,subprocess,sys
seen=set();failures=[]
for candidate in (sys.executable,shutil.which('python3'),shutil.which('python3.12'),shutil.which('python3.11')):
 if not candidate or os.path.realpath(candidate) in seen:continue
 seen.add(os.path.realpath(candidate))
 code='import sys;assert sys.version_info>=(3,10);'
 if sys.argv[1]=='1':code+='import venv,ensurepip;'
 code+='print(sys.executable)'
 try:
  result=subprocess.run([candidate,'-I','-S','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
  if result.returncode==0:print(result.stdout.decode().strip());break
  failures.append(candidate+': '+result.stderr.decode(errors='replace').strip().splitlines()[-1])
 except (OSError,subprocess.TimeoutExpired) as exc:failures.append(candidate+': '+str(exc))
else:raise SystemExit('没有可用的 Python（需 >=3.10，创建环境还需 venv/ensurepip）：'+ '; '.join(failures))
PYTHON_SELECT
)"
[ -n "$PYBIN" ] || { echo '容器内没有 python3'; exit 1; }
docker inspect "$CONTAINER" | python3 -c 'import json,sys; c=json.load(sys.stdin)[0]; t=sys.argv[1]; assert any(m.get("RW") and (t==m["Destination"] or t.startswith(m["Destination"].rstrip("/")+"/")) for m in c["Mounts"]), "工作目录没有持久化RW挂载，停止安装"' "$TARGET"
docker exec -w / "$CONTAINER" "$PYBIN" -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$PORT" || { echo "容器内 $PORT 端口已被占用（可能有其他 Runner 或服务）。可停掉旧 Runner 或换端口重装。"; exit 1; }

echo "== 2/4 拷贝网站、Runner 与数据集"
docker exec -w / "$CONTAINER" mkdir -p "$TARGET"/website "$TARGET"/scripts "$TARGET"/datasets "$TARGET"/logs "$TARGET"/results "$TARGET"/cache "$TARGET"/tmp "$TARGET"/dist
docker exec -w / "$CONTAINER" bash -c 'if [ -f "$1/scripts/runner.py" ]; then mkdir -p "$1/backups"; tar czf "$1/backups/before-setup-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$1" scripts website; fi' _ "$TARGET"
copy_tree() {
  # Docker's archive-copy endpoint can reject a writable submount beneath a
  # read-only /home or /mnt. Extract inside the container's mount namespace.
  tar -C "$1" -cf - . | docker exec -i -w / "$CONTAINER" tar -m -C "$2" -xf -
}
copy_tree "$HERE/website" "$TARGET/website"
copy_tree "$HERE/scripts" "$TARGET/scripts"
copy_tree "$HERE/datasets" "$TARGET/datasets"
if [ -d "$HERE/wheels" ]; then
  docker exec -w / "$CONTAINER" mkdir -p "$TARGET/cache/wheels"
  copy_tree "$HERE/wheels" "$TARGET/cache/wheels"
fi
COMPAT=("$PYBIN" -I -S "$TARGET/scripts/runtime_compat.py")
docker exec -w / "$CONTAINER" "${COMPAT[@]}" || { echo '容器线程检查失败，停止安装'; exit 1; }

echo "== 3/4 准备 AISBench 环境"
if [ -z "${3:-}" ]; then
  PIP_CERT_ARGS=()
  if [ -n "${ASCEND_BENCH_PIP_CERT:-}" ]; then
    docker exec -w / "$CONTAINER" test -r "$ASCEND_BENCH_PIP_CERT" || { echo '包源 CA 证书在所选容器中不可读'; exit 1; }
    PIP_CERT_ARGS=(-e "PIP_CERT=$ASCEND_BENCH_PIP_CERT")
  fi
  echo "   · 拷贝 AISBench 源码（约 70MB）..."
  docker exec -w / "$CONTAINER" mkdir -p "$BENCHMARK"
  copy_tree "$HERE/benchmark" "$BENCHMARK"
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
  # A persistent venv may point at Python from the previous container image.
  PYVERSION="$(docker exec -w / "$CONTAINER" "$PYBIN" -I -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  if docker exec -w / "$CONTAINER" "$VENV/bin/python" -I -c 'import sys,pip;assert "%d.%d"%sys.version_info[:2]==sys.argv[1]' "$PYVERSION" >/dev/null 2>&1; then
    echo "   · 复用本容器独立虚拟环境（$VENV）"
  else
    docker exec -w / "$CONTAINER" bash -c 'mkdir -p "$(dirname "$1")"; if [ -e "$1" ] || [ -L "$1" ]; then backup="$1.backup-$(date +%Y%m%d-%H%M%S)-$$"; [ ! -e "$backup" ] && [ ! -L "$backup" ] || exit 1; mv -- "$1" "$backup"; printf "   · 本容器虚拟环境失效或Python版本不兼容，已备份到 %s\n" "$backup"; fi' _ "$VENV"
    echo "   · 创建虚拟环境..."
    docker exec -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "${COMPAT[@]}" "$PYBIN" -m venv --system-site-packages "$VENV"
  fi
  # 每次都跑 pip 校验/补齐依赖（幂等：已满足的秒过，缺失的自动补上，避免旧装缺包）
  echo "   · 校验/补齐 AISBench 依赖（幂等，已装则秒过）..."
  ok=0
  for attempt in 1 2 3; do
    echo "   — pip 安装尝试 $attempt/3 —"
    if docker exec "${PIP_CERT_ARGS[@]}" -w "$BENCHMARK" -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_FIND_LINKS="$TARGET/cache/wheels" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "${COMPAT[@]}" "$VENV/bin/python" -m pip install --timeout 15 --retries 2 'setuptools<=80.10.2' wheel setuptools_scm 'nltk==3.8' \
       && docker exec "${PIP_CERT_ARGS[@]}" -w "$BENCHMARK" -e HOME="$TARGET" -e TMPDIR="$TARGET/tmp" -e PIP_CONFIG_FILE="$TARGET/pip.conf" -e PIP_FIND_LINKS="$TARGET/cache/wheels" -e PIP_CACHE_DIR="$TARGET/cache/pip" -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 "$CONTAINER" "${COMPAT[@]}" "$VENV/bin/python" -m pip install --timeout 15 --retries 2 --no-build-isolation "opencv-python-headless==4.11.0.86" -e . -r requirements/api.txt; then
      ok=1; break
    fi
    echo "   pip 失败，5 秒后重试"; sleep 5
  done
  [ "$ok" = 1 ] || { echo 'pip 安装三次失败，请检查所配置包源、代理和容器的可信 CA 证书'; exit 1; }
  echo "   ✓ AISBench 环境就绪"
  # sitecustomize 补丁装进 venv（请求超时 + 报错详情打进日志）
  _SP="$(docker exec -w / "$CONTAINER" bash -c "ls -d $VENV/lib/python*/site-packages | head -1")"
  docker exec -w / "$CONTAINER" cp "$TARGET/scripts/sitecustomize.py" "$_SP/sitecustomize.py"
fi
docker exec -w / "$CONTAINER" test -x "$AIS" || { echo "ais_bench 不存在或不可执行: $AIS"; exit 1; }
docker exec -w / -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 -e ASCEND_BENCH_API_ONLY=1 -e ASCEND_BENCH_PROCESS_ROLE=aisbench -e "PYTHONPATH=$TARGET/scripts" "$CONTAINER" "${COMPAT[@]}" "$AIS" --help >/dev/null || { echo 'AISBench 无法启动，请检查虚拟环境解释器和依赖；未按可用环境接入'; exit 1; }
AIS_PYTHON="${AIS%/*}/python"
docker exec -w / "$CONTAINER" test -x "$AIS_PYTHON" || AIS_PYTHON="$PYBIN"
docker exec -w / -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 -e ASCEND_BENCH_API_ONLY=1 -e ASCEND_BENCH_PROCESS_ROLE=aisbench -e "PYTHONPATH=$TARGET/scripts" "$CONTAINER" "${COMPAT[@]}" "$AIS_PYTHON" -c 'from ais_bench.benchmark.models.api_models import base_api; import threading; t=threading.Thread(target=lambda:None); t.start(); t.join(); print("AISBench 依赖加载及线程创建预检通过")' || { echo 'AISBench 依赖加载/线程创建失败，停止接入；请检查上方原始错误'; exit 1; }
docker exec -w / -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 -e ASCEND_BENCH_API_ONLY=1 -e ASCEND_BENCH_PROCESS_ROLE=runner -e "PYTHONPATH=$TARGET/scripts" "$CONTAINER" "${COMPAT[@]}" "$PYBIN" -c 'from transformers import AutoTokenizer' || { echo 'Runner 的 Python 无法加载 AutoTokenizer，性能数据生成依赖未就绪；未按可用环境接入'; exit 1; }
if [ -z "${3:-}" ]; then
  BENCHMARK_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("benchmark_id",""))' "$HERE/fleet-release.json")"
  docker exec -w / "$CONTAINER" "$PYBIN" -I -S -c 'import json,sys;from pathlib import Path;p=Path(sys.argv[1]);p.write_text(json.dumps({"benchmark_id":sys.argv[2]}))' "$TARGET/environments/$CONTAINER_ID/benchmark-release.json" "$BENCHMARK_ID"
fi

echo "== 4/4 写初始配置并启动 Runner"
HOST_IP="$(docker exec -w / "$CONTAINER" "$PYBIN" -c 'import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80));print(s.getsockname()[0])' 2>/dev/null || hostname -I | awk '{print $1}')"
docker exec -w / "$CONTAINER" "$PYBIN" -c 'import json,sys; from pathlib import Path; t,tok,model,host,port,ais,container=sys.argv[1:8]; cfg=dict(service=dict(host=host,port=int(port),model=model,tokenizer_path=tok),ais_bench_path=ais,gsm8k_path=t+"/datasets/gsm8k",container=container); p=Path(t+"/runner-config.json"); cfg.update(json.loads(p.read_text()) if p.exists() else {}); cfg.update(ais_bench_path=ais,container=container,installation_container_id=sys.argv[8]); p.write_text(json.dumps(cfg,indent=2))' "$TARGET" "" "" "$HOST_IP" "8000" "$AIS" "$CONTAINER" "$CONTAINER_ID"
docker exec -d -e ASCEND_BENCH_MANAGED="${ASCEND_BENCH_MANAGED:-0}" -e ASCEND_BENCH_HOME="$TARGET" -e ASCEND_BENCH_CONTAINER="$CONTAINER" -e ASCEND_BENCH_PORT="$PORT" "$CONTAINER" bash "$TARGET/scripts/start-runner.sh"
# docker exec inherits the container proxy environment, not start-runner.sh's.
# Probe loopback directly and allow a bounded startup interval.
ready=0
deadline=$((SECONDS + 30))
while ((SECONDS < deadline)); do
  if docker exec -w / "$CONTAINER" curl --noproxy '*' --fail -s --max-time 2 "http://127.0.0.1:$PORT/" >/dev/null; then
    ready=1; break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo 'Runner 在30秒启动窗口内未就绪，以下为启动诊断（不输出凭据）：'
  docker exec -w / "$CONTAINER" tail -n 80 "$TARGET/logs/runner.log" || true
  docker inspect --format '容器状态={{.State.Status}} OOMKilled={{.State.OOMKilled}} 内存限制={{.HostConfig.Memory}}' "$CONTAINER" || true
  docker exec -w / "$CONTAINER" python3 -I -S -c 'from pathlib import Path; import sys; base=Path(sys.argv[1]);
for name in ("runner.starting.pid","runner.pid"):
 p=base/name
 if not p.is_file():print(name+": 不存在");continue
 try:
  pid=int(p.read_text());proc=Path("/proc")/str(pid);stat=(proc/"stat").read_text() if proc.exists() else "进程已退出";wchan=(proc/"wchan").read_text() if (proc/"wchan").is_file() else "unknown";print(name+": "+stat+" wchan="+wchan)
 except (OSError,ValueError) as exc:print(name+": "+str(exc))' "$TARGET" || true
  exit 1
fi

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
