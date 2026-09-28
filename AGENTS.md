# 项目规则

- 产品只支持 Windows 游戏安装。Python 数据工具保持标准库实现，命令见 README。
- 当前会话负责主控。新 Agent 仅允许 Claude Code + Gemini，不启动 Codex/GPT 子任务，也不回退到 Claude 模型。
- 上游子模块与 `data/snapshots` 作为原始资料；译文写入独立任务目录，校验后在 `build` 合并。保留用户改动，不自动提交。
- 默认从官方 CDN 获取 KR，以公开版本服务发现资源版本；记录原文版本与中文 Release，不假定二者同步。Git KR 仅用于显式历史对照，更新 Git 前检查状态并拒绝覆盖本地改动。
- 新增文本 schema 必须纳入 diff 或列为未知字段复核，禁止静默忽略；源 ID 已删除时，不把旧原文条目补回新版中文。
- 不修改 ID、model、控制标记；保留占位符、标签和换行。韩文残留可能是术语或内部注释，先区分再翻译。
- 回归命令：`python3 -m unittest discover -s tests -v`。
- 安装脚本必须支持 `-WhatIf`、备份与失败恢复。没有 Windows 实测时不得报告安装验收通过。
