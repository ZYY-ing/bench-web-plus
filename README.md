# Bench Web Plus

基于 [ascend-bench-web](https://github.com/ZYY-ing/ascend-bench-web) 的多机 Runner 网页管理与模型评测工具。

管理员在网页连接服务器、选择容器、一键安装或接回 Runner；用户选择健康空闲的 Runner，配置已有 vLLM 服务，进行精度或性能测试。无需前端编译，不下载模型权重，不自动启动 vLLM。

当前版本是**本机控制台**：管理页和用户页分开，中心只监听 `127.0.0.1`。其他电脑可下载工具独立部署；尚不支持一个中心开放给多台电脑直接访问，也没有普通用户账号和结果隔离。

## 下载

- **推荐：[下载完整工具包](https://github.com/ZYY-ing/bench-web-plus/releases/latest/download/ascend-bench-fleet-v1-local.zip)**，解压即可启动，不需要 Git 或 Node.js。
- [发行版与 SHA256](https://github.com/ZYY-ing/bench-web-plus/releases/latest)：同一版本提供 ZIP、Runner 压缩包和校验文件。
- GitHub 页面选择 Code → Download ZIP，或 [下载 main 源码 ZIP](https://github.com/ZYY-ing/bench-web-plus/archive/refs/heads/main.zip)。源码内自带 Runner 压缩包，无需重新构建。

源码包约 7–8 MB，管理员 SSH 组件和服务器 AISBench 依赖在首次安装时另行下载，体积会明显更大。仓库不包含模型、服务器配置、管理员 token、运行数据库或测试结果。

## 安装前准备

| 位置 | 需要什么 |
|---|---|
| 管理员电脑 | Python **3.12+**（包含 pip）、现代浏览器、可以连接目标服务器 SSH；Windows / Linux 启动脚本均提供 |
| 目标 Linux 服务器 | Docker、Python 3、SSH 账号及相应 Docker 权限；已有兼容本地镜像或测试容器 |
| 测试容器 | host 网络、Python 3.12+、Bash、curl、专用持久化 RW 目录；可读的 tokenizer / 数据目录 |
| 模型推理服务 | 已独立启动的 vLLM OpenAI 兼容服务，`/health` 和 `/v1/models` 正常 |
| 同一电脑的普通用户 | 只需要浏览器，无需管理员凭据、服务器密码、Python 或本地 Docker |

管理员电脑不需要 Codex、Node.js、前端构建工具、Docker、vLLM 或 NPU 驱动。Runner 可与 vLLM 位于不同容器，但必须能访问推理服务和测试所需 tokenizer。只挂 tokenizer 即可，不必下载整份权重到电脑。

## 管理员部署教程

### 1. 下载并启动本机中心

解压到有写权限的独立目录，不要覆盖正在运行的旧目录。

Windows：双击 `start-fleet.cmd`，或在 PowerShell 中运行：

```powershell
.\start-fleet.cmd
```

Linux / macOS（提供脚本，实机验收环境见交接文档）：

```bash
bash start-fleet.sh
```

脚本首次把 `paramiko==5.1.0` 及其依赖装入当前目录 `.fleet-deps/`，不修改系统 Python。保持进程运行，浏览器打开：

- 管理员：<http://127.0.0.1:19881/>
- 用户：<http://127.0.0.1:19881/workbench/>

首次启动生成 `.fleet-data/console-token`。用文本编辑器打开，复制**文件里的完整字符串**到管理员登录框；不是填写文件路径。不要把管理员 token 交给普通用户。

需要更换本机端口：`start-fleet.cmd --port 19882` 或 `bash start-fleet.sh --port 19882`。指定 Python 可设置 `FLEET_PYTHON` 为解释器路径。

### 2. 网页连接服务器并部署 Runner

1. 点击「部署 Runner」，输入服务器 SSH 地址、端口、账号和本次密码。先核对服务器公钥指纹，首次确认后固定校验。密码仅用于内存会话，不保存到配置、日志或数据库；不读取个人 SSH 私钥或 ssh-agent。
2. 选择已有容器，或使用服务器**已有本地镜像**创建独立测试容器。新容器不带 NPU、不开 privileged、不拉镜像，也不启动模型服务。
3. 填专用持久化安装目录和 Runner 端口（默认 `18787`）。示例目录 `/home/ascend_bench/runner`；安装包缓存示例 `/mnt/share/ascend_bench/packages`。管理员需改成自己有权限使用的专用目录。
4. 外网环境保持「安装网络」为空；内网环境按下一节填写代理 / pip 源。
5. 点击「部署并启动」，观察日志与六步进度。工具自动校验缓存 SHA256，相同则上传 0 B，否则从电脑上传当前固定版本并校验；服务器无需从 GitHub 下载工具包。
6. 检查已安装版本：最新版已运行则复用，最新版未运行则启动；旧版只在身份核实、空闲且确认无测试进程时更新。启动所选停止容器会执行它原有启动命令。网络、挂载、端口不满足条件时停止并说明原因。
7. 六步绿色对勾表示部署完成。管理员测试环境卡片分别显示「SSH 管理通道」与「Runner 服务」状态，二者不是同一状态。

**网页模式无需手工建立 SSH 隧道，也无需用户输入 `runner-token`。** 中心通过已认证 SSH 会话转发请求；关闭中心或失去 SSH 会话后，管理员需重新连接。历史日志保留当时原文，新版安装日志会给出当前网页步骤。

「删除」只移除接入记录，不停止远端 Runner、不删除容器、文件或结果。用户列表与管理员当前测试环境一致；失败部署仅留在管理端部署记录，不会从历史中重新补回用户列表。仍登记但连接不可用的 Runner 保留显示且不可选择。

### 3. 内网 / 外网：下载与依赖安装

有两段独立网络：**电脑下载中心及 SSH 组件**，以及**服务器容器下载 AISBench 依赖**。给电脑设置代理不会自动给服务器提供可达的代理。

#### 外网可直连

直接下载 ZIP 并运行启动脚本。管理员组件由 pip 当前默认源下载；服务器 AISBench 默认使用 `https://mirrors.aliyun.com/pypi/simple`，也可在网页填写可达的 HTTPS 源。

#### 电脑只能通过代理下载

也可以在可联网电脑下载 ZIP 后拷贝过来。PowerShell 下载示例：

```powershell
$benchProxy = 'http://127.0.0.1:7897' # 替换为电脑自己的代理
curl.exe --proxy $benchProxy -fL 'https://github.com/ZYY-ing/bench-web-plus/releases/latest/download/ascend-bench-fleet-v1-local.zip' -o bench-web-plus.zip
# 首次安装本机 SSH 组件，可选其一或按网络同时配置：
$env:HTTPS_PROXY = $benchProxy
$env:PIP_INDEX_URL = 'https://pypi.org/simple' # 或电脑可达的内网 pip 源
.\start-fleet.cmd
```

Bash 对应方式：

```bash
export HTTPS_PROXY=http://127.0.0.1:7897 # 替换为电脑自己的代理
export PIP_INDEX_URL=https://pypi.org/simple
curl --proxy "$HTTPS_PROXY" -fL https://github.com/ZYY-ing/bench-web-plus/releases/latest/download/ascend-bench-fleet-v1-local.zip -o bench-web-plus.zip
# 解压、进入目录后：
bash start-fleet.sh
```

没有可用代理但 CDN 可达时，可下载整个源码 ZIP：

```bash
curl -fL https://codeload.github.com/ZYY-ing/bench-web-plus/zip/refs/heads/main -o bench-web-plus-source.zip
```

Runner 压缩包还提供 [jsDelivr 备用地址](https://cdn.jsdelivr.net/gh/ZYY-ing/bench-web-plus@main/dist/ascend-bench-fleet-kit.tar.gz)，但它**不是完整管理员工具包**；CDN 可能延迟，不用它判断 GitHub 是否已发布。

企业网络需要自签 CA 时，由管理员将可信 CA 配到电脑 / 容器，电脑 pip 可用 `PIP_CERT`、curl 可用 `--cacert`。本教程不要求关闭 TLS 验证。

#### 服务器只能访问内网代理 / 内网 pip 源

在部署向导的「安装网络」展开填写：

| 网页字段 | 例子（请替换） | 对应原版变量 |
|---|---|---|
| 服务器安装代理 | `http://proxy.example:3128` | `ASCEND_BENCH_PROXY` |
| 服务器 pip 源 | `https://mirror.example/simple` | `ASCEND_BENCH_PIP_INDEX` |
| 内网源域名映射，可选 | `192.0.2.10 mirror.example` | `ASCEND_BENCH_EXTRA_HOSTS` |

必须是**从服务器可达**的代理。服务器上的 `127.0.0.1` 指服务器自身，不能照抄电脑代理地址。地址不接受账号密码、查询参数或令牌；需要认证的源请由管理员预先在受保护环境配置。

沿用原版安装能力：HTTP 内网 pip 源可用并仅对该源设置 trusted-host；优先选择可用的 HTTPS 源。可选域名映射会写入**所选容器** `/etc/hosts`，不改宿主机 hosts；不需要就留空。

源码包通过 SSH 上传，因此服务器无需访问 GitHub。首次缺少的 Python 依赖仍要能访问所填 pip 源；无法访问时会如实失败，不会把 Runner 接入成功当成模型测试成功。

#### 完全离线

在**系统、架构和 Python 版本与管理员电脑相同**的联网环境下载 SSH 依赖：

```bash
python -m pip download -r requirements-admin.txt -d wheelhouse-admin
```

将工具包和 wheelhouse 拷到离线电脑，在工具目录执行：

```bash
python -m pip install --no-index --find-links wheelhouse-admin --target .fleet-deps -r requirements-admin.txt
```

然后正常启动。不要把 Windows wheels 用到 Linux，也不要跨 CPU 架构复制虚拟环境。

服务器必须已有可执行 AISBench 环境，或由管理员用与目标容器匹配的离线 wheels 预先安装到 `<Runner目录>/bench-venv`。默认位置 `<Runner目录>/bench-venv/bin/ais_bench` 可复用。**首次没有 AISBench、也没有依赖源 / 离线依赖时，不能完成安装**；源码包不捆绑全部评测依赖和镜像。

## 用户使用教程

1. 管理员在这台电脑启动中心、接入服务器后，打开 <http://127.0.0.1:19881/workbench/>。用户无需管理员 token 或 SSH 密码。
2. 在精度 / 性能页点击「刷新状态」，选择健康空闲 Runner，再点「使用所选 Runner」。列表显示地址、容器名；忙碌节点显示正在测试的 `IP:port` 和 `model_name`，不可用节点仍显示但不可选。
3. 填推理服务地址、端口、模型名（`/v1/models` 返回的 id）、Runner 容器能读到的 tokenizer 路径，点击「检查连接」。Runner 安装不会替你启动 vLLM。
4. 选择数据集，配置样本数 / 并发 / 输出长度，开始测试。确认 Runner 前后续步骤不可操作；后端也校验刷新与选择状态。
5. 「测试记录」查看和下载服务器真实结果，不再显示 Runner 选择步骤。记录属于 Runner 项目，尚无普通用户间结果隔离。

### 在另一台电脑使用

当前 `127.0.0.1` 永远指访问者自己的电脑。另一台电脑需要下载本工具、安装 Python，由管理员在该电脑启动中心并完成服务器接入；之后该电脑的用户只用浏览器。各电脑中心的接入清单和锁不共享，不能把它们当一个统一调度中心；同一个 Runner 的执行锁仍在远端生效。

如果希望「管理员部署一处，其他电脑只打开一个共享网址」，本版本尚未实现；需要另做 HTTPS、用户认证和访问控制。不要直接修改为 `0.0.0.0` 暴露当前管理端。

## 更新与排错

- 更新：停止本机中心，备份本机 `.fleet-data/` 和 `fleet-config.json`，用新工具包更新源码 / `dist/`，保留数据目录，再启动并重新连接 SSH。数据备份含管理员凭据，只能保存在受保护位置。
- 「最新版」以电脑下载的这份固定工具包为准，不自动执行 GitHub main 中未知更新。更新电脑工具包后，再从网页更新远端 Runner。
- 两个 host 网络容器在同一服务器共用端口；需要并行运行时使用不同 Runner 端口和独立持久化目录。
- 换容器复用原目录：当前启动容器名优先于旧配置；保留服务参数和结果。迁移已运行旧进程需核实 PID、命令、工作目录、启动环境及监听 socket 归属，且空闲并确认无测试进程。未知服务不会被停止。
- `TIME_WAIT` 退出残留允许正常地址复用，仍用原端口。真实监听冲突会报错，不自动换端口，不杀未知进程。
- SSH 已连接但 Runner 不可用：表示管理通道正常、Runner 服务异常，检查对应容器 / 项目 / 端口和具体错误。
- 退出管理员后管理按钮隐藏，部署进度轮询停止；用户会话没有部署和连接机器权限。

## 源码与验证

```bash
python -m fleet.build_kit  # 仅修改 Runner 源码后需要；下载使用不必执行
python -m unittest fleet.test_fleet
node fleet/test_picker.cjs # 开发验证可选；使用产品不需要 Node
python -m fleet.package
```

主要代码在 `fleet/`，内置 Runner 在 `dist/ascend-bench-fleet-kit.tar.gz`，补丁源码在 `fleet/runner-overlay/`。构建基于随仓库固定工具包覆盖补丁，不临时抓取上游 main。保留了旧接入实验的源码供回溯，默认网页只使用密码内存会话流程。

[脱敏会话交接文档](docs/HANDOFF.md)说明真实验收范围和已知限制。Runner 可用不代表 vLLM 模型可用，本仓库不宣称已经完成模型精度 / 性能验收。
