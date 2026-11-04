# 中心首次部署、重新配置与排障

适用于 v0.2.0-preview.1。示例地址和路径必须改成实际值；不包含实际机器凭据。

## 新电脑推荐顺序

1. 只有选定的中心电脑安装Python 3.12+（包含pip）与工具包；其他管理员和用户只需要浏览器。团队通常保留一个有效中心，不在每个管理员电脑重复安装。
2. 从本版本Release下载ZIP和SHA256文件，解压到本人可写的新目录。完整Python官网下载安装和PATH说明见 [共享中心部署说明](SHARED_CENTER.md)。
3. 双击 setup-shared-center.cmd。填写固定HTTPS网址、实际用户网段、首个管理员账号和密码；选择公司证书或生成测试证书。密码输入不显示字符或星号，输入后按回车，长度12–256字符。
4. 安装器创建当前Windows用户登录后运行的任务。确认任务运行、中心端口监听；管理员网页可关闭，但中心电脑仍需登录Windows、联网且不休眠。
5. 由有权限者按实际用户网段放行中心TCP端口。测试证书需在访问电脑信任 .fleet-data/tls/ca.pem；不跳过浏览器安全警告，不分发私钥或整个数据目录。
6. 用另一台电脑实际打开中心网址，确认免登录用户页可用；管理员登录接入服务器，随后退出管理员再验证用户刷新。
7. 新中心从空清单开始，不自动获取原电脑账号或服务器权限。重新经SSH核验并接入已有Runner；同一批Runner的测试使用一个中心，多个独立中心不共享中心侧占用记录。

## 常见问题

| 现象 | 原因和处理 |
|---|---|
| 双击后只剩 Press any key to continue | Python可能是Windows商店占位入口。安装真实Python，勾选 Add python.exe to PATH，关闭旧窗口后重开，验证 python --version 和 python -m pip --version。新版启动器会明确报缺少解释器。 |
| PowerShell不认识 py | py启动器没有加入当前进程PATH；新开终端或用python.exe完整路径。使用下方FLEET_PYTHON方式，不必重复安装工具。 |
| 两次密码不一致 | 新版会原地重试。确认键盘布局、大小写及输入法，两次输入同一密码；不要把密码发到日志、命令参数或聊天。 |
| 重新打开安装器仍用旧账号 | 已有账号会保留。改密码运行 fleet.admin reset 账号名；重设密码不是重新部署中心。 |
| 任务已创建，随后报XML编码错误 | 旧预览对schtasks管道编码处理有误。使用本版本 fleet/service.py，并在原安装目录运行 fleet.service start；不用重建账号/证书。 |
| 同名任务归属检查拒绝 | 新版会解析Windows账号到SID。如果仍拒绝，应核对任务是否属于另一个用户或安装目录，不应删掉未知任务或绕过检查。 |
| Python 3.14报 Missing Authority Key Identifier | 旧测试证书缺少严格TLS字段。本版本新生成证书已修复；仅替换代码不自动改已有CA。首次部署可按下方备份重新初始化，再信任新的ca.pem。已有团队中心应安排证书迁移，不能关闭验证。 |
| 网页本机可打开，其他电脑打不开 | 检查填写的是中心可达IP而非127.0.0.1、端口监听、防火墙、实际客户端源IP是否在允许网段、证书是否可信，以及Wi-Fi/VPN是否隔离客户端。中心能连服务器不等于用户能连中心。 |
| 安装目录缺少持久化RW挂载 | 所填容器内目录不属于该容器的可写持久挂载。按下节核对现有挂载，不重建其他人的容器、不关闭保护。 |

已安装但命令找不到Python时，在PowerShell中使用实际路径（下面是占位示例）：

```powershell
$env:FLEET_PYTHON = 'C:\Path\To\Python\python.exe'
& $env:FLEET_PYTHON --version
& $env:FLEET_PYTHON -m pip --version
.\setup-shared-center.cmd
```

该环境变量只在当前终端有效；后台任务会记录实际解释器路径，无需全局设置。

## 地址、网段和端口怎么填

中心网址示例 https://192.0.2.10:19885，冒号使用英文字符。IP必须属于中心电脑，其他用户电脑到该地址实际可达，端口未被其他服务使用。DHCP地址变化需要固定租约或重新配置地址及证书。

允许网段是访问用户电脑的实际来源网段，不是服务器网段，也不是随意填写的范围。192.0.2.0/24 表示该子网；192.0.2.80/32 仅允许单个IPv4地址。多个网段用英文逗号分隔，不允许0.0.0.0/0。中心允许范围和防火墙范围应一致；有VPN/NAT时按中心实际收到的源IP判断。

| 示例端口 | 所在位置 | 作用 |
|---|---|---|
| 19885 | 中心电脑 | 管理员/用户浏览器访问的HTTPS网页 |
| 22 | 服务器 | 部署和维护使用的SSH |
| 19787 | 服务器宿主机 | 中心日常访问的受限TLS连接组件；需要凭据，不是个人电脑的SSH隧道端口 |
| 18787 | Runner所在服务器/host容器 | Runner自身回环接口，由连接组件内部访问 |
| 8000 | 模型服务所在机器 | 已有vLLM接口示例，与上述端口独立 |

用户日常只访问中心网址。不同服务器可用相同连接端口；同一服务器接入多个Runner应使用分别未占用的Runner端口与连接端口。未知占用不会自动抢占或换端口。

防火墙示例仅供有权限者在确认实际地址后执行；不要直接照抄示例IP，不要关闭防火墙：

```powershell
New-NetFirewallRule -DisplayName 'BenchWebPlus HTTPS 19885' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 192.0.2.10 -LocalPort 19885 -RemoteAddress 192.0.2.0/24 -Profile Any
```

## 已有容器安装目录报错

在服务器只读核对所选容器：

```bash
docker inspect <容器名> --format '{{json .Mounts}}'
```

Destination 是容器内路径，Source 是宿主机路径，RW 必须为 true。Runner安装目录应为一个可写挂载的专用子目录；开启持久连接时，还须能确认它对应的宿主路径。例如已有 Source=/home/your-user/aisbench_web、Destination=/work/bench、RW=true，则容器内安装目录可用 /work/bench，不能把宿主路径误填为容器路径。

界面示例 /home/ascend_bench/runner 不保证存在于每个已有容器挂载中，安装包缓存目录也必须改为自己有权限使用的共享盘专用目录。安装包上传成功不代表Runner安装成功；持久挂载检查失败时安装流程会停止。

## 改密码、重配网络、从头部署

以下命令均在中心安装目录执行。能使用python时执行python -m；否则使用 & $env:FLEET_PYTHON -m，先按上文设置真实解释器路径。

仅重设已有管理员密码：

```powershell
& $env:FLEET_PYTHON -m fleet.admin reset admin
```

向导交互输入两次新密码，不需要删除配置或重装服务。旧登录会失效。

重新配置网址/网段前，先执行 fleet.service stop 停止当前中心，再次运行 setup-shared-center.cmd，会保留已有账号。测试证书会重新签发服务证书；跨电脑访问仍需检查证书和网络。不要在旧进程仍运行时把安装器的启动提示当作新配置已生效。

真正从空白重新部署中心：

```powershell
& $env:FLEET_PYTHON -m fleet.service stop
& $env:FLEET_PYTHON -m fleet.service remove
```

确认两条命令成功后再备份数据目录：

```powershell
Rename-Item -LiteralPath '.fleet-data' -NewName ('.fleet-data.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
.\setup-shared-center.cmd
```

不要盲删其他目录或未知后台任务。备份含账号校验值、接入凭据和私钥，只留在受保护位置，不上传。重置中心不会停止或删除服务器容器、Runner、连接组件或结果；旧服务器接入应由管理员核对后重新登记。新的中心CA需要重新信任；也不要把旧电脑整个 .fleet-data 发给其他用户。
