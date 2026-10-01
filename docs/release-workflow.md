# 发布工作流与状态总结 (2026-10-01 动态发布架构，正式发布 2026092802.da704a71.ad3c2822)

## 动态发布链路 (已实现，恢复与验收进行中)
工作流已实现动态解耦 DAG (`workflow.json`)，摆脱对静态版本（如 `2026092802`）与固定 25 条 / `58f4876f` 哈希的硬编码依赖。当前代码与门禁已实现，待主控在真实工作流与真实 Windows 游戏环境下进行最终验收。

### 核心阶段流转
1. **prepare (Command 节点)**:
   - 韩文数据源：仅通过 SSH (`windows`) 从 Windows 游戏实际路径 `F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data\Assets\Resources_moved\Localize\kr` 提取（`scripts/windows_source.py`），生成 `KR/` 快照与 `provenance.json`（记录 `source_hash` 与 `raw_version`）。旧 Git 子模块与官方 CDN 下载已停用，`text_data/LocalizeLimbusCompany` 子模块已按要求删除。
   - 中文数据源：拉取最新 LLC Release 中文包（`scripts/llc_snapshot.py`），生成 `LLC_zh-CN/`、`LICENSE_UPSTREAM.txt` 与 `provenance.json`。
   - 幂等判断：比对线上公开 `latest.json` 中的 `source.raw_version`（含 `windows-sha256:<hash>` 格式）与 `chinese_release`。若完全匹配，脚本退出码 10，直接命中 exit_routes 进入 `$complete` 结束，不唤起后续 Agent。
   - 分片编排：若有更新，运行 `localization.py diff`，将待译词条按完整文件结构与字符预算划分至各个独立分片目录，生成独立的 `input.json`、`glossary.json` 和严格任务提示词 `prompt.txt`。

2. **translate (Command 节点)**:
   - 使用 `scripts/herdr_translation.py translate` 接入 Herdr 原生 `default` 会话。
   - 动态窗格池管理与恢复机制：默认以 6 并发度在受信任的项目根工作区管理窗格，支持通过恢复 registry（记录各分片 `agent_name`、`pane_id` 及校验哈希 `input_sha`）进行任务恢复。通过 Herdr 的 `agent wait` 与 `agent get`（或分步派工跟进）完成状态轮询与产物读取，而非仅盲目依赖单次 `prompt --wait`。
   - 输入不可变性与产物收集：Agent 运行前后校验 `input.json` 哈希不变，产物 `translations.json` 经语法与字段格式校验通过后安全收集并清理窗格。

3. **review (Command 节点)**:
   - 正式入口已切换为 `scripts/review_batches.py`（旧 freeform review 工具保留作参考但不是正式入口；旧 `scripts/validate_review.py` 已 fail closed 阻断）。
   - 默认调度 6 个独立 Gemini Agent 按不重叠批次并行校对（每批 50 条），用户无需每次显式指定并行；每批由控制器写入输入、提示词、响应与日志哈希凭据（receipt）。
   - 弹性与故障恢复策略：
     - 单批次错误或异常仅隔离故障工人（quarantine），其余健康工人继续消费队列中的待审任务，避免单点偶发超时中断全盘；任何最终未完成或未决（unresolved）批次坚决阻断合并与发布。
     - 根级快照与不可变基准（`diff.json`、`translations.json`、`draft_translations.json`）一旦发生哈希篡改或文件缺失，必须通过 `RootInputIntegrityError` 立即触发全局致命阻断（global poison halt），全部工人立即停止领取任务并保留现场证据，严禁放宽哈希约束。单个 `batch input.json` 损坏仅作普通批次错误隔离处理。
     - 批次完整复用机制：在启动或重试时，若所有待审批次均已有通过 SHA256 与覆盖率校验的合法 receipt，直接跳过 worker 窗格的创建与初始化，消除冗余开销。全部 cache 命中时将 `pool_progress.json` 顶层状态显式写入 `completed` 覆盖旧状态，但只要存在未决（unresolved）项严禁标为 `completed`。
     - 状态检查与防盲发机制：每批次在发送 prompt 前（包括首次派发与重试）必须调用 `get_agent_status`。仅在状态为 `idle` 或 `done` 时允许发送；若处于 busy（`working` / `unknown` / `blocked`），经有界等待后若仍未 settled 则判定失败，严禁盲目重派。
     - 真实计时口径与进度落盘：每批次详细记录真实耗时，严格区分 wallclock（实际挂钟时间）与所有并行批次耗时之和（并发累计耗时）；失败批次的耗时和尝试次数由 `error_receipt.json` 记录；初始化异常时进度状态直接落 `failed`。
     - 历史状态核查与进程存活：工作池状态以原子文件落盘（`pool_progress.json`），包含当前控制器 `pid` 与 `updated_at`；注意历史 `running` 状态仅表示上一次刷新时仍在执行，必须结合操作系统真实 PID 核验存活，不可仅盲信文件中的 `running`。
     - 主控执行约束：主控发现自身陷入连续重复读/规划且无实质产物时，必须立即缩小任务范围或更换执行者；遇到未跟踪（untracked）文件直接读取内容，严禁仅凭 `git diff` / `git grep` 做出判断。
   - 故障恢复执行说明（Manual Recovery）：
     - 恢复前必须先确认旧 controller 进程已停止运行（通过 PID / `ps` 核实），且所有 workers 已处于 `settled`（`idle` 或 `done`）状态。
     - 严格保留已冻结的 `draft_translations.json` 及其元数据与失败现场证据文件。
     - 严禁盲目自动重跑全量任务或触发发布。
     - 恢复时通过 `scripts/dev-workflow` 的 command 节点执行以下命令（动态传参，禁止硬编码历史路径）：
       ```bash
       python3 scripts/review_batches.py --run-dir "${RUN_DIR}" --max-workers 6
       ```
   - 严格遵循已核实的 LLC 译名证据约束（统一称为「LLC 译名」，严禁称为「游戏官方汉化」）：如 `원레그`=单脚人，`간수`角色名=看守（狱警仅在对应旧语境允许），`죄책감`=负罪感（严禁误写），禁止无确凿证据改为独腿、狱卒等个人偏好用语。
   - 机制上支持 registry 复用与冻结 draft 断点续跑，对译文及所有 diff 待复核项合并后统一进行 LLC 译名及结构验收。
   - 严禁轻信 Agent 的机械 progress 汇报，任何未决（unresolved）项在 validate 节点坚决阻断发布。

4. **validate (Command 节点)**:
   - 全链路防篡改与完整性校验（`scripts/translation_pipeline.py validate`）。
   - 强校验 `diff.json`、各分片 `input.json`、合并后的 `translations.json`、`reviewed-translations.json` 的 SHA256 哈希绑定。
   - 校验所有待复核条目均具有合法 action（`keep_current` / `approved_as_is` / `internal_dummy_ignored` / `empty_intentional` / `format_verified`）且 `resolved: true`。若存在未决项或需补译项，门禁阻断发布。

5. **package (Command 节点)**:
   - 动态版本计算：`<LLCtag>.<source_hash[:8]>.<review_hash[:8]>`。
   - 严格检查并保留字体目录（包括空 `Font/Title` 目录及 `Font/Context` 内有效字体文件），包含 snapshot `LICENSE_UPSTREAM.txt`。
   - 生成 `latest.zip` 与 `package_receipt.json`。

6. **release (Approval 节点)**:
   - 人工审批后触发 `publish_cloudflare.py` 与 `activate_release.py`。
   - 上传前预检使用独立 probe query（`?probe=check`），彻底消除 clean URL 的 404 缓存污染。
   - 同版本不同内容拒绝覆盖；发布后更新兼容 schema 1 的公开 `latest.json`。

---

## 当前实跑状态与遗留问题
- **正式发布完成**：正式主发布 run `20261001-195806-0ce694` 已成功完成，目标版本 `2026092802.da704a71.ad3c2822`。
  - 数据源基准：Windows 本地游戏实际路径韩文快照 2406 JSON（`windows-sha256:da704a715ddaa2663d9d551193f44109f7e425d4747bfa89b24515aacd70d80c`）与最新上游 LLC 2026092802。
  - 审校规模：4484 项待翻译与 632 项 diff 差异项全量完成校对，采用 6 并发独立 Gemini Agent 按 50 条批次执行，包含 19 项术语修正（如 `원레그`=单脚人，`간수`角色名=看守等）。
  - 发布产物：语言包 ZIP 大小 18,286,125 字节，SHA256 为 `bdb7cf1abfbb7a9a2ded668974f35ae5587f69315f3c5220c189493b191b6bd6`；更新器 ZIP 大小 8,206 字节，SHA256 为 `ecbdf2ef84a5b5b86f7b19659fb8fbf28ea1ff7743c762126e3b482fdf6e4bb8`。
- **官网与导航站上线验收通过**：
  - 通过 EGO Lite 浏览器访问 `https://limbus-cn.deadfish.win/`，页面完整版本 `2026092802.da704a71.ad3c2822`、原文版本 `Windows · da704a71`、中文基础 `2026092802` 正确展现。
  - 手动下载按钮与 Windows 更新器按钮均可见、可点击且指向最新 CDN 链接；在真实页面中点击两处下载按钮触发真实下载，保存的语言包与更新器 ZIP 文件 SHA256 及大小均与线上 `latest.json` 严格一致。
  - 同 TaskSpace 内对导航站 `https://deadfish.win/` 进行了正式域名入口核验，入口完整有效。
  - 视觉截图在 `Page.bringToFront` 后单次重试依然遇到 CDP `Page.captureScreenshot` 超时，已在回执如实记录未通过。
- **发布后幂等性实测验证**：发布后通过实际运行 run `20261001-201034-1d3887` 真实再次通过 SSH 拉取 Windows 源及 LLC 快照，成功触发 exit code 10 (`up_to_date`)，未产生冗余任务，幂等性验证通过。
- **Windows 真实安装实测全量通过**：
  - 通过正式验收工作流 run `20261001-201406-f3058e`（receipt: `windows_acceptance_apply_receipt.json`）在 Windows 物理机上实测完成端到端安装验收。
  - 自动发现游戏路径：不传入任何目录参数，自动化脚本通过注册表/Steam 库自动定位至 `F:\SteamLibrary\steamapps\common\Limbus Company`。
  - 原文件备份校验：旧 2297 个文件完整备份至 `LimbusTranslationBackups/LLC_zh-CN-20261001-201548-254c1be7`，且全量 Hash 校验通过（`BackupHashVerificationPassed: true`）。
  - 新版本文件安装：新 2339 个语言包文件路径集合及 SHA256 全量比对通过（`PackageFullHashVerificationPassed: true`，`InstalledFilesCount: 2339`）。
  - 安装后生效版本：`InstalledVersion` 确认为 `2026092802.da704a71.ad3c2822`。
  - 二次运行幂等验证：再次运行提示 already latest (`2026092802.da704a71.ad3c2822`) 且文件存在，未产生冗余 backup（`IdempotentSecondRun: true`）。
  - WhatIf 安全性验证：WhatIf 模式 2299 个状态元素完全无改写，无破坏性副作用。
- **官网 JS 兼容部署实测成功**：通过工作流最小部署 run `20261001-155733-67d33e`，已成功将 `site/script.js` 部署至线上生产环境。
- **边界与待办严格区分**：
  - **已实测通过**：Windows 更新器下载、解压、目录自动探测、旧版 Hash 校验备份、新版 2339 文件 Hash 严格覆盖安装、版本写入及二次运行幂等性。
  - **尚未实测**：游戏实际启动（Launch）与游戏内各界面字体/UI 实际渲染显示；因此官网页面上的「测试版」（Beta）与验证待完成提示暂予严格保留，不提前宣称游戏内通过。

## 线上状态与最新资源 (2026-10-01 已发布版本)
- **线上发布版本**: `2026092802.da704a71.ad3c2822`
- **历史归档版本**: `2026092802.58f4876f`
- **官网**: [https://limbus-cn.deadfish.win/](https://limbus-cn.deadfish.win/) (主官网动态读取 `latest.json` 展现数据)
- **CDN**: `limbus-cdn.deadfish.win`
- **最新清单 (latest.json)**: 本地权威路径为 `<run_dir>/build/public/latest.json`（根目录 `build/` 仅为本地执行历史镜像或兼容占位），线上为 `https://limbus-cn.deadfish.win/latest.json`。

### 优化验证边界

本轮使用离线故障注入与回归测试验证调度行为；尚未用新一轮真实翻译测量提速，不给出预计缩短到多少分钟的承诺。进程被关闭或会话中断后，不会自动唤醒恢复。发现重复读取或规划且无产物时，主控应收窄任务、重建上下文或交接；交接只转移文件所有权，禁止擅自回滚已完成的改动。

## LLC 参考记忆先检索后翻译规范 (Translation Memory Retrieval Rules)

1. **检索与置信层级**：翻译与校对前必须先检索已核实的 LLC 既有快照记忆，按「确证专有名词 > 完整同句 > 核心短名称 > 同文件前文上下文 > 相似词句候选（须核对语境）」层级参考。
2. **防虚假对齐与排除项**：纯位置数组（无唯一稳定 ID/key 的整型下标）及同列表中出现重复 ID 的整组条目不得作为可靠事实证据；中文快照中未翻译或韩文残留条目一律严格剔除。
3. **出处记录与证据链**：所有检索到的参考例句在各分片/批次中输出为 `context.json` 并记录证据 ID 与出处；校对与翻译严格按语境选择，若 LLC 存在多义旧译须依具体上下文取舍。
4. **争议冲突与不可擅断**：若参考记忆与词表存在不可调和的矛盾或证据不足以确信，翻译阶段在独立 `term_notes` 记录疑点，校对阶段必须明确标为 `unresolved`（Pending 批次）或将 action 设为 `needs_translation` 且 `resolved: false`（Review 批次），严禁脑补臆断。
5. **缓存强绑定与历史边界**：批次与分片缓存严格校验 `context_sha` 与 `index_sha`，任何参考索引或证据变更均使旧缓存自动作废拒绝复用；历史已发布的旧语言包尚未进行重译与全面刷库。
