# 公开发布范围

公开仓库只保存源码、维护文档、回归测试和当前可分发安装包。地址示例使用文档保留网段 `192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`；域名使用 `.example`。真实机器、代理、内部镜像、账号及本机目录通过本地配置提供。

不要提交 `fleet-config.json`、`.fleet-data`、`.fleet-deps`、`.env`、Runner 登录凭据、私钥、数据库、虚拟环境、代理配置、原始执行日志、任务归档或本地管理脚本。公开实验报告使用匿名代号。发布前同时检查源码、压缩包内部和文件所有者信息；wheel 保持第三方原始字节并校验完整性，公开依赖的版本号、许可证署名和测试假凭据不是本项目秘密。

运行包不携带上游单元测试、冒烟测试、Python 缓存和发布上传脚本；仓库有效回归测试保留。上游许可证、运行源码、必要依赖和允许分发的数据集保留。修改 overlay 后运行 `python -m fleet.build_kit`，更新安装包及摘要。仓库发布不自动替换正在运行的中心或 Runner。

仅修改当前分支不能删除 Git 历史、旧 Release 附件、缓存或他人克隆中的内容。发现真实凭据泄露时应先撤销或轮换；历史清理需协调提交引用和现有克隆。参见 [GitHub 敏感数据清理说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。
