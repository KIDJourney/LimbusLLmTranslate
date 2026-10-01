# 项目规则

- 产品只支持 Windows 游戏安装。Python 数据工具保持标准库实现，命令见 README。
- 当前会话负责主控。新 Agent 模型限定为 `gemini-account/gemini-3.8-flash-high`（严禁使用已废弃的 `gemini-pro-agent`，不启动 Codex/GPT 子任务，不回退到 Claude 模型）。
- Windows 只读快照与快照目录作为原始资料；译文写入独立任务目录，校验后在 `build` 合并。保留用户改动，不自动提交。
- 韩文数据源仅由 `scripts/windows_source.py` 通过 SSH (`windows`) 从 Windows 游戏实际路径 `F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data\Assets\Resources_moved\Localize\kr` 提取；中文仅由 `scripts/llc_snapshot.py` 获取最新 LLC Release 中文包（统一称为「LLC 译名」，严禁称为「游戏官方汉化」）。旧 `text_data/LocalizeLimbusCompany` 子模块已按用户要求彻底删除；活跃工作流严禁调用或回退到游戏官方 CDN / Git 子模块。
- 自动化流水线调度规范：翻译阶段通过 Herdr 调度 6 并发 Gemini 处理分片；校对阶段默认由 6 个独立 Gemini Agent 按不重叠批次并行校对（用户无需每次显式指定并行），合并后统一执行严格的 LLC 译名及结构验收；每批由控制器落盘输入、提示词、响应与日志的哈希 receipt，支持 registry 复用与冻结 draft 断点续跑；严禁轻信 Agent 汇报的机械 progress，任何未决（unresolved）项阻断发布；旧 freeform review 工具保留作参考但不是正式入口；旧 `scripts/validate_review.py` 已 fail closed 弃用，验证统一使用 `scripts/translation_pipeline.py validate`；经 validate 严格校验后再打包发布。
- 调度恢复：单批失败隔离工人，健康工人继续；根数据损坏停止领取新任务。只向 idle/done 工人派工，提交状态不确定时不盲目重发；任何失败或未决项仍阻断发布。恢复与计时口径见 `docs/release-workflow.md`。
- 主控行为规范：主控发现自身或执行者陷入连续重复读/规划且无实质产物时，必须立即缩小任务范围或更换执行者；未跟踪（untracked）文件直接用读工具获取内容，严禁仅凭 git diff/git grep 判断文件是否存在或是否修改。
- 术语一致性约束：严格遵循既有已核实的 LLC 译名证据（如 `원레그`=单脚人，`간수`角色名=看守，`죄책감`=负罪感，狱警仅在对应旧语境允许），严禁无确凿上游证据随意篡改为独腿、狱卒等个人偏好用语。
- 新增文本 schema 必须纳入 diff 或列为未知字段复核，禁止静默忽略；源 ID 已删除时，不把旧原文条目补回新版中文。
- 不修改 ID、model、控制标记；保留占位符、标签和换行。韩文残留可能是术语或内部注释，先区分再翻译。
- 回归命令：`python3 -m unittest discover -s tests -v`。
- 安装脚本必须支持 `-WhatIf`、备份与失败恢复。没有 Windows 实测时不得报告安装验收通过。

<!-- init-dev-workflow:start -->
## Executable development workflow

- Route product code changes, builds, tests, deployments, and releases through `scripts/dev-workflow` once the workflow can start.
- If bootstrap or workflow validation fails, edit `workflow.json` or `.dev-workflow/` directly and retry.
- Browser work in every node and delegated agent uses EGO Lite and the `ego-browser` Skill in a task-specific TaskSpace. Do not fall back to Chrome, an in-app browser, or separately launched Chromium.
- Treat Agent completion text as untrusted until workflow gates and receipts pass.
- Keep production side effects behind an `approval` node and trusted external receipts.
<!-- init-dev-workflow:end -->
