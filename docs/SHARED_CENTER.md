# 共享中心部署说明（v0.2.0-preview.1）

中心安装在一台常开个人电脑上。普通用户免登录打开统一网址；管理员使用各自账号登录管理页。关闭管理员网页或退出管理员不会关闭中心和 Runner 连接。

先下载本版本 ZIP 并解压到新目录。[首次部署排障与从头配置](CENTER_TROUBLESHOOTING.md)汇总 Python、密码、后台任务、证书、网段和容器挂载问题。

## 安装要求

| 位置 | 需要安装 |
|---|---|
| 中心电脑 | Python 3.12+（含 pip）和共享中心工具包 |
| 其他管理员电脑 | 仅现代浏览器，不需要第二套中心、Python、SSH 或 Docker |
| 普通用户电脑 | 仅现代浏览器，不需要管理员密码、服务器密码或测试环境 |
| 测试服务器 | 已有 SSH、Docker、Python 3、host 网络测试容器和专用持久化目录；模型服务另行准备 |

无需 Node.js、前端构建或外部数据库服务。SSH 组件仍安装在工具目录 `.fleet-deps`，不修改系统 Python。不下载模型权重，不自动启动 vLLM。

## 中心电脑首次配置

### 先安装 Python（Windows，只在中心电脑安装）

1. 打开官方 Windows 下载页：https://www.python.org/downloads/windows/ 。选择 Python 3.13 的正式版本，点击该版本下面的 **Download Windows installer (64-bit)**。常规 Intel/AMD Windows 电脑使用此安装包，不要选择 embeddable package、源码包或预发布版。
2. 双击下载的安装程序，在首页勾选 **Add python.exe to PATH**，再点击 **Install Now**。保留默认的 pip 组件即可，通常安装到当前用户，不必安装给所有用户。
3. 安装完成点击 Close，关闭之前打开的命令窗口。
4. 按 Win+R，输入 cmd 并回车，分别执行 `python --version` 和 `python -m pip --version`。应显示 Python 3.12 或更高版本及 pip 版本；如果跳转商店、没有输出或找不到命令，说明还没配置好，先不要继续中心安装。
5. 关闭验证窗口，双击工具目录里的 `setup-shared-center.cmd`。

中心电脑无法访问外网时，可在另一台联网电脑下载上述官方 Windows 安装程序，复制到中心电脑后安装；后续 SSH 依赖离线准备见本文“外网、内网与离线安装”。其他管理员和普通用户只用浏览器，无需安装 Python。

### 再配置中心

1. 解压 `bench-web-plus-shared-preview.zip` 到本人有写权限的独立目录，安装 Python 3.12+。
2. Windows 双击 **`setup-shared-center.cmd`**。
3. 填中心固定 HTTPS 地址、允许访问的用户网段、首个管理员账号和密码。
4. 选择已有公司可信证书，或者生成本中心测试证书。
5. 安装器为当前 Windows 用户建立登录后启动的后台任务，并启动中心。

地址示例 `https://192.0.2.10:19881`；网段按实际网络填写，如 `192.0.2.0/24`。多个允许网段用逗号分隔。地址和网段由管理员明确填写，程序不自动扫描本机网络，不自动修改防火墙、电源设置或系统证书信任库。

请确认中心电脑不休眠、不断网，并只向需要使用的内网开放中心端口。共享模式必须使用 HTTPS、明确允许网段并已有管理员账号；不接受直接向全部互联网开放匿名用户入口。

### 证书与用户电脑

有公司认可的可信证书时，用户直接打开网址，无需安装任何运行环境。使用测试证书时，用户首次需要信任 `.fleet-data/tls/ca.pem` 这个公钥证书；不要跳过浏览器证书警告。

**不能分发** `ca-key.pem`、`server-key.pem`、`channels.json` 或整个 `.fleet-data`。这些只留在中心电脑，并限制备份访问权限。

### 后台运行条件

后台任务使用当前用户身份和最低权限，不保存 Windows 登录密码。Windows 用户登录后自动运行，锁屏可以继续；注销、关机、休眠或断网会影响中心。

需要无人登录 Windows 也运行的环境，应由系统管理员配置受管系统服务。本预览版不擅自建立机器级服务或配置自动登录。

若后台任务安装权限不足，可以先双击 `start-fleet.cmd` 前台运行并保留窗口，再由有任务管理权限的管理员完成安装。不覆盖属于其他目录或用户的同名任务。

维护命令，在中心目录执行：

```powershell
python -m fleet.service status
python -m fleet.service start
python -m fleet.service stop
python -m fleet.service remove
```

`remove` 仅移除本工具后台启动任务，不删除项目、账号、Runner 或结果。日志为 `.fleet-data/center.log`，过大的旧日志保留一份轮换备份。

## 管理员部署和维护 Runner

1. 打开中心网址，点击“管理员登录”，用独立账号和密码进入管理页。
2. 点击“部署 Runner”，填写服务器 SSH 信息和本次密码，核对公钥指纹。
3. 选择已有容器或已有本地镜像，填写专用项目目录、Runner 端口（默认18787）和受限连接端口（默认19787）。
4. 保持“启用受限持久连接”勾选；按网络需要填写安装代理和 pip 源，提交部署。
5. 相同安装包复用缓存、上传0 B；否则自动上传并校验。检查源码版本并复用可执行 AISBench，只有身份核实、空闲且无测试进程的旧 Runner 才会更新。
6. 六步绿色对勾表示部署接入成功。管理员可以退出网页，用户日常访问不依赖管理员登录。

选中停止容器后会执行其原有启动命令。新建容器只使用服务器已有镜像，不挂 NPU、不使用 privileged、不启动 vLLM。目录、端口和其他服务不能盲目覆盖。

### 持久连接

部署仍使用本次密码 SSH。部署后在选定项目目录下增加 `center-access/<连接ID>/`，启动只接受受限评测 API 的 TLS 连接组件。

日常链路：用户浏览器 → 中心 → 服务器受限 TLS API → 本机回环 Runner。

- 不保存服务器 SSH 密码，不读取个人 SSH 私钥，不修改 SSH 授权文件或 sshd 配置。
- 中心仅保存工具专用 API 凭据和证书信任信息。组件没有远程命令、安装、Docker 管理或任意文件读写接口。
- 中心需能访问服务器受限连接端口；未知端口占用会报错，不抢占、不自动换端口。
- 中心重启后，已接入 Runner 可直接刷新和使用。服务器或容器本身重启、连接组件被停止时，由管理员按需重新部署恢复。
- 连接组件证书有效期一年。管理员可重新通过已核验 SSH 部署以更新组件和证书；无法确认进程归属或 Runner 状态时不会强制停服。

删除测试环境仅移出接入清单，保留服务器进程、安装目录、连接文件和结果；它不是远端卸载或凭据销毁。新电脑下载源码不会自动继承已有接入权限。

### 多管理员

“管理员账号”界面支持添加、停用、启用和重置密码。当前管理员权限相同，密码只保存加盐 scrypt 校验值。操作记录只记录操作者、时间和接口，不记录请求密码。

停用、重新启用或重置密码后，旧登录不会恢复。至少保留一个可用管理员。一个管理员退出不会影响其他管理员或普通用户。

全部密码忘记时，在中心电脑本地交互维护，密码不进入命令行历史：

```powershell
python -m fleet.admin create admin2
python -m fleet.admin reset admin1
python -m fleet.admin disable admin2
python -m fleet.admin enable admin2
```

首次本机也可用 `.fleet-data/console-token` 的完整字符串进入管理页创建账号。创建首个正式管理员后，初始凭据和它的旧登录立即失效。共享模式必须先有正式管理员。

## 普通用户使用

1. 打开同一个中心网址，默认就是用户界面，不需要等待管理员先登录。
2. 未部署时显示空列表；不可用 Runner 保留并说明原因。清单与管理员当前测试环境一致，不从历史失败记录补回已删除环境。
3. 点击刷新，选择健康空闲 Runner 并确认，再按原流程配置推理服务与测试参数。忙碌条目显示测试目标和模型名。
4. 未选择 Runner 时后续步骤不可用，提交时中心和 Runner 还会校验占用。普通用户不能停止其他用户发起的任务，管理员可排障接管。
5. 测试记录保持原工作台布局。仍是 Runner 项目共享记录，没有普通用户账号或结果隐私隔离。

不设20人硬上限，不会因为第21个人进入就挤掉其他会话。后台限制同时处理的请求数量、合并极短时间内重复查询，以保护资源；这不等于限制能打开网页的人数。

中心重启后用户任务归属保留到会话过期，但旧的选择和刷新状态失效，需要重新选择。浏览器 Cookie 清除或匿名会话过期后，旧任务需要管理员接管。网络结果未知时，不按TTL盲目释放占用。

## 外网、内网与离线安装

分开看两段网络：中心电脑安装 SSH 组件，服务器容器安装 AISBench。

外网可达时直接运行安装器。内网电脑可以在有网环境下载 ZIP 再拷贝，首次 pip 按实际环境设置代理或源：

```powershell
$env:HTTPS_PROXY = 'http://代理地址:端口'
$env:PIP_INDEX_URL = 'https://可达的pip源/simple'
.\setup-shared-center.cmd
```

HTTP 内网 pip 源还需设置 `PIP_TRUSTED_HOST` 为明确可信的源域名，优先HTTPS。企业CA可用 `PIP_CERT`，不需要关闭TLS验证。电脑代理不会自动成为服务器可达的代理。

完全离线时，在与中心电脑同系统、架构和Python版本的联网环境准备 wheels：

```bash
python -m pip download -r requirements-admin.txt -d wheelhouse-admin
# 拷贝工具与wheels至离线电脑后：
python -m pip install --no-index --find-links wheelhouse-admin --target .fleet-deps -r requirements-admin.txt
```

不要跨 Windows/Linux、CPU架构或Python ABI复制虚拟环境。

服务器侧在网页“安装网络”填写服务器可达的代理、pip源和可选域名映射，沿用原版安装能力。不接受地址中嵌入的账号密码或查询令牌。

| 字段 | 示例，按实际替换 |
|---|---|
| 服务器代理 | `http://proxy.example:3128` |
| pip源 | `https://mirror.example/simple` |
| 可选域名映射 | `192.0.2.20 mirror.example` |

只有明确填写映射时才修改所选容器 `/etc/hosts`，不改宿主机 hosts。工具包由中心 SSH 上传，服务器无需访问GitHub。完全离线的服务器必须已有可执行 AISBench 或匹配的离线依赖；源码包不捆绑所有wheels、镜像和模型。

## 本地调试与验证范围

本机 HTTP 调试仍可运行 `start-fleet.cmd --port 19883`，默认只监听回环。正式共享必须配置 HTTPS 与允许网段；调试端口不能当作局域网共享入口。

本次按用户最新要求验收管理员与用户管理 Runner 的功能。保留原评测逻辑，不做真实 vLLM 模型精度/性能验收，不把受控接口回放当作模型测试结果。

```bash
python -m fleet.setup_admin
python -m unittest fleet.test_fleet fleet.test_shared
node fleet/test_picker.cjs
python -m fleet.package --name bench-web-plus-shared-preview.zip
```

[实现与验收交接](HANDOFF.md)。后台任务定义已检查，实际 Windows 任务注册权限、公司证书和第二台个人电脑的浏览器互访仍需在正式中心环境确认。
