# 80 网段 Runner 部署故障修复与验证（2026-11-05）

两台服务器的专用 `ascend-bench-runner` 已恢复健康、空闲。仅操作该工具容器内的 Runner 程序和独立依赖目录；未停止、删除或重启容器，未修改 Docker 守护进程、其他人的容器或 vLLM。验证未发送推理请求。

## 原因与修复

| 机器 | 实际原因 | 修复 |
| --- | --- | --- |
| 192.0.2.137 | Runner 启动时通过 `sitecustomize` 提前加载 AISBench 及大量本地扩展；首次 HTTP 请求创建处理线程时发生原生段错误。以前的“已启动”横幅早于绑定端口，容易误判成功 | 管理进程不加载 AISBench 超时/错误补丁，补丁保留在评测子进程；先监听并记录 PID，再打印成功；后台启动使用 exec 主进程；失败自动附启动诊断 |
| 192.0.2.155 | 脚本优先选择 `/usr/bin/python3.12`，该解释器缺少 ensurepip/pip；默认 Python 3.11 完整。改正后进一步发现 Docker 20.10.8 的 seccomp 对 clone3 返回 EPERM，与容器 glibc 2.39 的线程实现不兼容，连标准库单线程测试也失败 | 优先选择默认且满足 venv/ensurepip 的解释器；在线程确实失败且确认 clone3 被 EPERM 拒绝时，为当前进程叠加有限兼容过滤器，继续拒绝 clone3，但返回 ENOSYS，让 glibc 回退到原有受限 clone；子进程继承 |

线程兼容方式保留现有 seccomp 过滤，设置 no_new_privs，不开放额外系统调用，不使用 privileged 或 seccomp=unconfined。不兼容的其他失败继续报错，不自动修改系统配置。安装、pip、AISBench 可执行检查、Runner 启动及后续评测子进程均覆盖此路径；正常运行时不添加过滤器。

该兼容问题的上游说明：[Moby #42681](https://github.com/moby/moby/pull/42681)。127 的原生崩溃由 faulthandler 抓取到 `threading.Thread.start`；这里通过隔离管理进程依赖消除触发条件，没有宣称修复第三方扩展本身。

## 实测结果

两台机器都完成部署后的身份、版本和空闲状态复核；再次部署可以识别最新代码并复用当前进程，不重复安装依赖。145 实际补齐了专用 Python 3.11 虚拟环境中的 AISBench 依赖，AISBench `--help`、AutoTokenizer 导入和 Runner HTTP 均通过。

| 机器 | 状态请求 | 并发数 | 持续时间 | 失败 | 中位耗时 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 192.0.2.137 | 240 | 8 | 60 秒 | 0 | 6.69 ms | 9.22 ms |
| 192.0.2.155 | 240 | 8 | 60 秒 | 0 | 6.79 ms | 9.76 ms |

上表从服务器本机请求带认证的 Runner `/api/status`，验证 HTTP 线程和持续刷新稳定性，不是跨网络延迟。两台测试中 Runner PID、实例 ID、容器启动时间均未改变，每次状态均为健康、空闲。另有部署客户端经 SSH 连续复核状态。短测不能证明数天运行绝不失败。

本地 Python 回归 117 项通过；前端 Runner 选择及管理员部署测试通过；Shell 语法及 diff 格式检查通过。

无凭据原始结果位于 `D:/aisbench_web/outputs/80-deployment-20261105/`。工具包 SHA256 为 `5c22763c962c9bf8efac7a01e569e6f5f0191992d4d1eb4822054cedee042e44`。

## 后续网页部署使用新代码

工作区源码、工具包及两台远端 Runner 已修复。本次直接验证没有改写中心的机器登记、授权凭据或受限持久通道。

若当前中心运行自受保护的 `C:\ProgramData\BenchWebPlus`，还需将中心服务更新为工作区版本，后续网页部署才会使用新的安装后复核逻辑。当前代理对该系统目录无访问权限，未尝试绕过权限或强制结束服务。请在 **Windows 管理员 PowerShell** 执行一次：

```powershell
cd D:\aisbench_web\bench-web-plus
$fleetPython = 'C:\Users\example-user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $fleetPython -X utf8 -m fleet.service stop --boot
& $fleetPython -X utf8 -m fleet.service install-boot
```

更新器等待旧中心释放端口，并保留系统目录内现有机器登记、密钥和数据库。随后在管理员页面对两台机器点击部署/核验，以中心现有授权完成登记与持久通道核对；Runner 已是最新版时会复用进程。
