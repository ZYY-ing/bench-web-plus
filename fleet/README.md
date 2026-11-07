# Fleet 实现说明

管理员和用户的完整部署教程、内外网配置、离线限制与故障处理见[仓库 README](../README.md)。

实现与验收交接见[脱敏交接文档](../docs/HANDOFF.md)。

当前行为、历史回退与长会话完整交接见[2026-11-07 交接补充](../docs/SESSION_HANDOFF_20261107.md)。修改 `runner-overlay/` 后务必运行 `python -m fleet.build_kit` 更新随包部署的 `dist/`。
