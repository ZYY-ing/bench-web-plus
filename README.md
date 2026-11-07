# Bench Web Plus · 共享评测中心

一个中心统一管理多台 Linux Docker Runner。管理员通过浏览器接入服务器、安装 Runner；用户通过同一 HTTPS 网址选择 Runner，评测已有的 OpenAI 兼容模型服务。

**新部署请使用当前 `main` 的完整源码。旧 Release 是历史快照，不包含本次会话的全部修复。** 中心无需 Node.js、前端编译、Docker 或 NPU；其他管理员与用户的电脑仅需浏览器。

- [中心部署详细说明](docs/SHARED_CENTER.md) · [首次部署排障](docs/CENTER_TROUBLESHOOTING.md)
- [当前交接与验证边界](docs/HANDOFF.md) · [文档索引](docs/README.md)
- [匿名化工具与原生 AISBench 对照实验](docs/experiments/2026-11-07-aisbench-tool-native-ab.md)

## 工作方式

```text
管理员 / 用户浏览器 → HTTPS 中心 → 受限 TLS API → Runner
                         │                     └→ AISBench → 模型服务
                         └→ SSH：安装、维护、自动恢复
```

中心不逐条代理模型推理请求。Runner 在服务器 CPU 上准备数据、启动 AISBench、收集日志与结果；模型服务独立运行，可以在另一台可达的机器。工具不会自动启动 vLLM，也不下载模型权重。

团队通常部署一个中心。其他电脑打开同一个网址即使用同一清单和授权，无需重复输入服务器密码；另行部署第二个中心不会自动继承原中心数据和权限。

## 代码仓结构

| 路径 | 用途 |
|---|---|
| `fleet/app.py`、`http_server.py` | 中心 API、用户会话、占用与任务调度 |
| `fleet/accounts.py`、`admin.py`、`configure.py` | 管理员账号、首次 HTTPS 与网段配置 |
| `fleet/service.py`、`setup_shared.py` | Windows 后台任务、开机服务、安装向导 |
| `fleet/static/` | 管理页、Runner 列表、存储清理界面 |
| `fleet/session_ssh.py`、`managed_ssh.py`、`management_host.py` | 首次 SSH 授权、后续受控维护、分块传输 |
| `fleet/persistent*.py`、`gateway.py`、`recovery*.py` | 受限 TLS 评测通道、断线恢复及服务器恢复服务 |
| `fleet/direct_worker.py`、`replacement.py`、`pip_trust.py` | Runner 安装、停止旧容器后的迁移、包源与 CA 检查 |
| `fleet/dataset_request.py`、`storage_cleanup.py` | 生成任务与服务互锁、管理员手动清理 |
| `fleet/runner-overlay/scripts/` | Runner、数据生成、缓存锁、运行时适配、日志读取与远端清理源码 |
| `fleet/runner-overlay/website/` | 精度、性能、执行结果与测试记录页面 |
| `fleet/runner-overlay/wheels/` | 少量固定依赖 wheel；不是完整离线环境 |
| `dist/ascend-bench-fleet-kit.tar.gz` 与 `.sha256` | 网页部署实际上传的固定 Runner 包及校验文件；包含固定 AISBench 源码和基础数据 |
| `fleet/build_kit.py`、`package.py` | 重建 Runner 包、生成完整中心 ZIP |
| `fleet/test_*.py`、`fleet/test_*.cjs` | 本地回归测试，不连接生产模型服务 |
| `docs/` | 部署、交接、验收与实验记录 |

运行后才生成的 `.fleet-data/`、`.fleet-deps/`、`fleet-config.json` 不属于源码。前者可能包含数据库、证书与加密授权，不能上传或分发。机器专用维护脚本、测试输出和原始归档不随源码发布。

## 新机器部署中心（Windows）

### 1. 准备环境并下载当前源码

安装完整 Python **3.12+**（包含 pip），确认 `python --version` 与 `python -m pip --version` 正常；不要使用 Windows 商店占位入口。当前共享中心与 Windows 无人登录开机方案有实机使用记录；其他系统不要直接照搬 Windows 服务步骤。

在后续操作使用的 PowerShell 中固定解释器路径，下面的维护命令会使用它：

```powershell
$env:FLEET_PYTHON = (Get-Command python -CommandType Application).Source
& $env:FLEET_PYTHON --version
```

如果 Python 没在 PATH 中，直接将第一行右侧改成带引号的实际 `python.exe` 完整路径。

有 Git：

```powershell
git clone https://github.com/ZYY-ing/bench-web-plus.git
cd bench-web-plus
```

无 Git：下载 [main 源码 ZIP](https://github.com/ZYY-ing/bench-web-plus/archive/refs/heads/main.zip)，解压到有写权限的独立目录。源码已含 Runner 包，**普通部署无需自行构建**。

只有中心电脑安装 Python。需要中心到服务器 SSH 和受限 TLS 端口可达、用户浏览器到中心 HTTPS 端口可达，Runner 到模型 HTTP 接口可达。

### 2. 首次配置

在工具目录运行：

```powershell
.\setup-shared-center.cmd
```

向导安装固定 SSH 依赖到 `.fleet-deps/`，依次填写中心固定 HTTPS 地址、允许访问的用户 CIDR 网段、首个管理员账号及密码、已有可信证书或测试证书，然后创建并启动当前用户登录后台任务。

示例网址 `https://192.0.2.10:19885`、网段 `192.0.2.0/24` 仅用于说明，必须改成实际地址。中心地址不要填用户电脑自己的 `127.0.0.1`。密码交互输入时不显示字符；不应放在命令参数里。

没有 `py` 启动器时，可指定真实 Python 路径：

```powershell
$env:FLEET_PYTHON = 'C:\Path\To\Python\python.exe'
& $env:FLEET_PYTHON --version
.\setup-shared-center.cmd
```

PowerShell 执行引号中的程序路径必须在前面加 `&`；设置环境变量不会让不存在的 `py` 命令变可用。

由有权限者仅向所需用户网段放行中心端口。使用测试证书时，访问电脑信任 `.fleet-data/tls/ca.pem` 公钥证书；不分发私钥，不跳过 TLS 校验。中心电脑需联网、不休眠。

### 3. 需要无人登录 Windows 也运行

首次向导默认是“用户登录后启动”，锁屏可以运行，注销后不能保证运行。需要开机即运行时：

```powershell
& $env:FLEET_PYTHON -X utf8 -m fleet.service stop
```

确认中心正常停止后，**以 Windows 管理员身份**从同一个源码目录运行 `install-center-autostart.cmd`。也可以在已提升权限的 PowerShell 执行：

```powershell
& $env:FLEET_PYTHON -X utf8 -m fleet.service install-boot
& $env:FLEET_PYTHON -X utf8 -m fleet.service status --boot
```

安装器将代码、Python 与中心数据复制到受保护的 `%ProgramData%\BenchWebPlus`，由 SYSTEM 开机运行，不保存 Windows 密码。成功后运行数据位于受保护副本，原源码目录的数据不再是运行中的数据源。

维护开机任务，使用管理员 PowerShell：

```powershell
& $env:FLEET_PYTHON -X utf8 -m fleet.service status --boot
& $env:FLEET_PYTHON -X utf8 -m fleet.service stop --boot
& $env:FLEET_PYTHON -X utf8 -m fleet.service start --boot
```

未安装开机任务时去掉 `--boot`。前台启动的中心用原窗口 Ctrl+C 停止。不要同时启动前台、登录任务和开机任务的多个中心；端口未释放时先核对旧实例，不结束未知进程。

### 4. 首次验证

另一台电脑打开设置的网址，确认默认用户页可访问；管理员登录后部署一个专用 Runner，退出管理员再验证用户选择 Runner。不要仅以“计划任务创建成功”判断端口、证书、服务器接入全部成功。

## 管理员使用

1. 登录管理页，点击部署 Runner，填写 SSH 地址、端口、账号及本次密码，核对主机指纹。保留“首次授权后自动管理”和“受限持久连接”，完成工具专用密钥授权。
2. 选择已有测试容器，或用服务器已有本地镜像新建容器。新建容器不挂 NPU、不启用 privileged、不拉镜像、不启动模型。选择停止的已有容器会执行其原有启动命令，应先确认它是可操作的测试容器。
3. 填专用安装路径、Runner 端口、受限连接端口与安装包缓存目录。新容器默认只读挂载宿主机 `/mnt`、`/home`，安装目录另行可写挂载；已有容器不能自动补挂载。
4. 按需要填服务器可达的代理、pip 源及可信 CA。中心电脑的代理不等于服务器代理；证书失败应修复 CA/包源，不关闭 HTTPS 校验。服务器首次缺依赖时仍需可用的依赖来源。
5. 提交后观察部署日志。程序检查实际容器、目录、端口、AISBench 可执行性与版本；相同包缓存命中不重复上传，缺失时分块上传并校验。不会每次从 GitHub 拉取未知版本。
6. 部署成功后清单持久保留，关闭管理员网页不会断开后台连接。首次授权后 SSH 自动重连、中心启动后恢复管理连接；服务器恢复还依赖容器及独立恢复服务正常。主动停止的外部容器不会被擅自启动。

同机只保留一个 Runner，换容器时先手动停止**专用旧 Runner 容器**，再选择新容器复用路径和端口。程序确认旧容器停止/删除、端口无未知监听后迁移；失败时回退程序和配置，旧容器保持停止。不要停止带有他人模型服务的容器。

同机多个 Runner 必须使用不同 Runner 端口、受限连接端口及独立持久目录；路径别名映射到同一宿主目录也会被检查。多 Runner 共享 CPU、内存、网络，不是性能隔离。

“删除接入记录”不等于卸载容器、销毁密钥或删除远端文件。账号管理支持添加、停用和重置管理员。忘记密码时在中心所在环境交互运行 `python -m fleet.admin reset <账号>`，不要重新初始化已有数据。

### 手动清理存储

管理页点击清理，填写保留天数（1–3650，默认 7），扫描所有登记 Runner，查看路径、名称、大小，默认全选后可取消勾选再清理：

- 性能缓存范围 `datasets/perf/`，最近生成/校验复用会更新 `.last-used`；正在生成、被任务引用或占用未确认释放的缓存跳过。
- 任务范围 `results/jobs/real-*`，删除的是整个任务目录。按任务结束时间与目录内最新修改时间中的较新者判断，运行中不删除，查看/下载不续期。
- 执行删除前重新验证状态、签名和目录指纹。忙碌、SSH 断开、Runner 不可用或文件已变化时拒绝/跳过并报告；不绕过锁清理。
- 删除结果后对应记录从测试记录页消失。没有每日自动清理，也不另存轻量历史摘要；需要的结果先下载另存。旧环境、备份、模型及任意非正式目录不在此按钮的通用清理范围。

## 用户使用与生成内容

1. 打开中心网址，进入精度或性能。刷新列表，选择健康空闲 Runner 并确认；提交时还会重新核验，刷新时的空闲不是资源预留。
2. 填目标模型服务 IP/Host、端口、`served-model-name` 与 Runner 可读取的 tokenizer 路径，检查连接。允许跨机器测试，以实际 HTTP 可达性为准，不以 ping 代替接口检查。
3. 精度选择 GSM8K、GPQA Diamond 或 AIME2026；性能填写输入 token 长度、请求数、输出长度、随机种子及共享前缀比例，再生成或复用数据集。大数据生成有异步进度，不能仅凭列表“数据准备中”判断卡死。
4. 配置并发、温度、最大输出、接口 Chat/Completions、流式、ignore_eos、预热、超时与 retry。预热输出默认跟随正式最大输出，可单独改；预热不计入正式指标。最大输出可填到 225280 tokens（220 Ki），并发可填 1000，实际承载能力由模型服务与资源决定，支持填写不代表已实测该规模。
5. 精度仅提供 `all`，性能仅提供 `perf`。`perf` 配合额外参数 `reuse` 时，保留 AISBench 原生转为结果可视化的行为；不能在额外参数中重新覆盖 mode。
6. 点击测试，查看统一执行日志、真实配置、命令和原生结果。任务成功/失败后，确认进程组退出即自动恢复空闲；仍有活跃进程或状态未知时保留占用。
7. 在测试记录查看并下载结果。完整日志在 Runner，不受网页日志尾部显示上限影响；原生 ASCII 表格在比例字体编辑器中可能错位，可查原生 CSV/HTML 等文件。

| 功能 | 生成/保留什么 |
|---|---|
| 性能数据集 | `data.jsonl`、manifest 与缓存校验信息；同目录写入互锁，tokenizer 只读不加锁 |
| 每次正式任务 | 独立任务目录、`record.json`、`config.py`、真实命令、含运行环境的 `run.sh`、`execution.log`、AISBench 原生输出 |
| 精度 | 预测、失败请求、评分与 summary（以原生任务实际产出为准） |
| 性能 | 原生性能表、逐请求详情、图表及可用稳态结果（以实际产出为准） |
| 状态/测试记录 | Runner 与目标服务信息、原生退出状态、提示及文件索引；普通用户之间没有结果隐私隔离 |

### 参数规则

额外参数用逗号分隔的 `参数名=值`。网页已出现的温度、并发、输出、超时、预热等直接在表单填，不在额外参数重复配置。“格式与支持参数”动态列出三类完整名单：

| 类别 | 例子 | 生效位置 |
|---|---|---|
| 模型配置 | `request_rate=2`、`trust_remote_code=false` | `models[0]` 顶层 |
| 生成参数 | `top_p=0.95,top_k=20`、`skip_special_tokens=true` | `models[0].generation_kwargs` |
| AISBench 执行参数 | `debug=false,max_num_workers=2`、`reuse="YYYYMMDD_HHMMSS"` | 对应 CLI 参数 |

若有其他未收录的**生成参数**，填 `generation_kwargs.参数名=JSON值`，如 `generation_kwargs.reasoning_effort="low"`；是否支持由目标模型服务决定。该前缀不用于任意新增模型顶层参数或命令行选项，不支持任意 Python 代码。两种别名不能重复配置；debug 开启时遵循原生单 worker 行为。

请求超时默认使用 AISBench 原生不限时，也可自定义秒数；`retry` 原值透传，不加一转换。`max_model_len` 遵循 AISBench/服务原生规则，工具不额外截断、填充精度题目或增加上下文拦截。

性能“稳态指标”使用原生稳态算法，样本/并发时间形态不足可能无法形成稳态窗口；“pressure”按时长持续发请求，实际请求数可超过数据集条数。两者独立配置，不保证打开就一定能产生稳态统计。

任务状态遵循 AISBench 原生退出结果；失败请求、缺少评分或长度差异另外提示，不修改原生指标，不因期望长度与服务统计略有不同自行判失败。复核应同时查看退出码、失败文件和原生结果，不能仅看页面“完成”。

共享前缀缓存复用还取决于输入/输出长度、条目数、种子、前缀比例、接口、tokenizer 指纹及缓存版本；只匹配文件夹名不足以复用。不同 Runner 只读同一 tokenizer 不互锁；共用缓存的写入会互锁。同一 Runner 的生成与测试互斥，同一中心对同一模型服务的生成/测试按占用规则拒绝冲突，不会替你探测和阻止所有外部 AISBench 客户端。

## 更新、构建与验证

新中心从空配置开始，不携带本仓库作者的服务器授权。已有中心更新前备份真实运行数据目录；Windows DPAPI 加密授权不能简单复制到另一台电脑当作已授权。

普通后台中心先正常停止、更新源码与 `dist/`、保留数据，再启动。SYSTEM 开机中心先以管理员权限正常停止 `fleet.service stop --boot`，然后从原安装源目录重新运行开机安装器；更新时保留受保护副本现有账号与授权。仅更新工作区文件不会更新已经复制到 ProgramData 的运行中心。

中心更新不等于远端所有 Runner 已更新。对空闲 Runner 从管理页按已有容器重新部署，核验目标版本；正在执行任务的 Runner 待任务结束后更新，不强制重启。无需每个用户手动配参数或重新安装 Python。

开发/发布验证（Node 仅用于开发测试）：

```powershell
python -m fleet.build_kit
python -m unittest discover -s fleet -t . -p 'test_*.py'
Get-ChildItem fleet -Filter 'test_*.cjs' | ForEach-Object { node $_.FullName; if ($LASTEXITCODE) { throw "前端测试失败" } }
python -m fleet.package --name bench-web-plus-shared-current.zip
```

`build_kit` 将本地审阅过的 overlay 覆盖到固定包，不在线抓取上游 main；wheel 保持二进制原样。当前 Runner 压缩包约 39 MB，主要包含固定 AISBench 源码、基础数据和少量兼容依赖，不含模型与完整 Python/torch 环境，不能按历史 7 MB 描述。

内网/离线的中心依赖准备、服务器 CA 和恢复边界见[部署说明](docs/SHARED_CENTER.md)。包源不可达、镜像 Python 不兼容、模型上下文不足等仍会如实失败，部署成功也不等于任意模型与所有参数组合都已验收。
