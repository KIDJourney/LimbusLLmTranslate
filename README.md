# LimbusLLmTranslate

- [发布工作流与状态总结](docs/release-workflow.md)

面向 Windows 版《边狱公司》的中文补译工具。使用游戏自带的自定义语言接口，安装到 `LimbusCompany_Data\Lang\LLC_zh-CN`。

- [Windows 安装、回滚与来源](docs/windows-install.md)
- [9 月原文更新与修正结果](reports/20260928-september.md)；早期 5 条补译包已撤回。
- `scripts/cycle_stage.py`：翻译与校对阶段的薄调度器。自动扫描同父目录的历史 run 筛选有效基线（通过 `validate_and_freeze_baseline` 且 LLC 树哈希一致）；无有效基线时自动 fallback 全量翻译与全量校对；存在有效基线时记录 `baseline-selection.json` 并调用增量翻译与增量校对；校对时严格复核基线防篡改，并自动提取 pending 名称中与 LLC 规范译名冲突项作为 `force_review_items.json` 送入复审。
- `scripts/translation_pipeline.py`：动态发布流水线核心（已实现，真实全流程验收进行中），提供 `prepare`（基于 Windows SSH 与 LLC 快照、分片与提取术语）和 `validate`（哈希绑定、防篡改与 review disposition 逐项校验）。
- `scripts/review_batches.py`：正式校对控制器。默认调度 6 个独立 Gemini Agent 按不重叠批次并行校对（每批 50 条），每批写入输入、提示词、响应和日志哈希凭据，合并后统一进行 LLC 译名及结构验收，支持 registry 复用与冻结 draft 断点续跑；旧版 freeform review 工具保留但已非正式入口。
- `scripts/herdr_translation.py`：Herdr 原生 `default` 会话控制交互式 Claude Code (Gemini)，按动态窗格池并发执行翻译分片调度与管理。
- `scripts/package_translation.py`：动态发布打包（版本格式 `<LLCtag>.<source_hash_short>.<review_hash_short>`），严格检验字体与快照授权，保留空目录。
- `scripts/publish_cloudflare.py`：Cloudflare R2 上传与最新清单生成，避免预检 404 缓存污染，校验哈希防覆盖。
- `scripts/localization.py`：字段级 diff、词条校验与合并。Python 3.9+，仅标准库。
- `scripts/Install-LimbusTranslation.ps1`：Windows PowerShell 5.1+ 安装脚本。

## 动态交付工作流 (Stage B，正式发布)

通过 `scripts/dev-workflow` 启动工作流 DAG：
```bash
./scripts/dev-workflow start --workflow workflow.json --workspace . --request "动态发布交付"
```

流水线节点流转：
1. **prepare**：调用 `windows_source.py` 通过 SSH (`windows`) 从 Windows 游戏实际路径 `F:\SteamLibrary\steamapps\common\Limbus Company\LimbusCompany_Data\Assets\Resources_moved\Localize\kr` 提取韩文快照，调用 `llc_snapshot.py` 获取最新上游发布的 LLC 中文快照（统称「LLC 译名」，非游戏官方汉化）。若线上版本与本地快照完全一致，退出码 10 直接流转到 `$complete`。若有变更，计算 diff 并按文件和字符预算划分为独立翻译分片，提取术语表。
2. **translate**：调用薄调度器 `cycle_stage.py translate`。自动扫描同目录历史 run 寻找通过校验且 LLC 树哈希一致的基线：无有效基线时自动回退至全量分片翻译；存在有效基线时写入 `baseline-selection.json` 并调用 `incremental_translation.py` 复用已校对译文为草稿，仅对增量/变更词条调度 Claude Code (Gemini) 进行独立分片翻译。
3. **review**：调用薄调度器 `cycle_stage.py review`。若无有效基线则直接调用 `review_batches.py` 进行 6 并发全量多 Agent 校对；若存在基线选择则重验基线防篡改及 LLC 树一致性（拒绝静默更换），根据 TM 提取 pending 中与 LLC 规范译名冲突的名称作为 `force_review_items.json` 送入 `incremental_review.py`，进行冲突关联新审与增量校对。
4. **validate**：严格校验 diff、input、translations、reviewed 完整哈希链路，检查所有分片覆盖率，确保所有 review 项均有合法非空的明确理由并标记为已解决。
5. **package**：生成动态版本号并打包 `build/latest.zip`，严格检查字体及授权协议。
6. **release**：人工审批节点，通过后发布至 Cloudflare R2 并激活上线。

当前运行状态：正式已发布 `2026092802.da704a71.ad3c2822`，基于 Windows KR 2406 JSON 与 LLC 2026092802 快照；4484 翻译项与 632 差异项均经 6 并发独立 Gemini Agent 逐批校对完成，含 19 项术语修正；语言包产物 ZIP 大小 18,286,125 字节，SHA-256 为 `bdb7cf1abfbb7a9a2ded668974f35ae5587f69315f3c5220c189493b191b6bd6`。官网与导航站 DOM 校验及实际两个下载按钮文件哈希全部通过（视觉截图因 CDP 超时未通过）；发布后通过 `20261001-201034-1d3887` 真实再拉源验证命中 exit code 10 (`up_to_date`) 幂等完成。Windows 物理机真实更新与安装已通过 dev-workflow (`20261001-201406-f3058e`) 实测通过（自动发现 F 盘游戏目录、旧 2297 文件备份至 `LLC_zh-CN-20261001-201548-254c1be7` 且全量 Hash 一致、新 2339 文件路径与 SHA 全量通过、InstalledVersion 为 `2026092802.da704a71.ad3c2822`、二次运行 already latest 无新增备份、WhatIf 2299 状态元素无改写）；但游戏实际启动与游戏内画面渲染尚未实测，官网测试版（Beta）提示暂予保留。

## 更新与 diff (底层工具)

在项目根目录执行。每次使用新快照目录，脚本拒绝覆盖已有快照。

```powershell
python scripts/localization.py update --output data/snapshots/latest
python scripts/localization.py diff --source data/snapshots/latest/KR --chinese data/snapshots/latest/LLC_zh-CN --output reports/diff-latest.json
```

韩文数据源仅通过 SSH 从 Windows 本地游戏目录提取，中文从 LLC 最新 Release 下载。记录版本、发现来源、时间与 SHA-256；按目录保留文件，只去掉原文文件名的 `KR_` 前缀。

历史说明：旧版的官方 CDN 下载与 Git 子模块更新已彻底废弃并移除，活跃流水线中禁止调用；`text_data/LocalizeLimbusCompany` 子模块也已按要求删除。

如有上一版原文，加上 `--previous-source <上一版KR目录>`，把原文变更列入复核清单。中文和韩文内容不同本来就是翻译结果，不会因此被标成待翻译。

匹配使用相对路径、带类型的 ID、重复 ID 出现次序和嵌套字段；没有 ID 的数组按索引匹配，需要关注上游数组重排。`TEXT_KEYS` 覆盖剧情、技能、flavor、RPG 对话、任务目标和字符串数组；未知韩文字段会进入复核，不静默遗漏。ID/model 不翻译；中韩混合术语、演出注释、空白、内部标签和占位文本进入 `review`。这些规则属于保守筛选，仍需人工审校。

## 交给 Claude Code / Gemini

```powershell
python scripts/localization.py prepare-agent --manifest reports/diff-latest.json --output data/agent-work/latest
```

任务目录含 `input.json`、术语表和 `task.txt`。用已配置 Gemini 的 Claude Code 执行该任务，只写 `translations.json` 和 `notes.md`。不得启动其他 Codex/GPT Agent，不得回退到 Claude 模型；凭据通过现有配置读取，不写入仓库。

家庭委派使用 Herdr 右侧交互式 Claude Code，在已配置 Gemini token 通道的环境中以 `agent start --kind claude` 启动，显式传入 `--model gemini-account/gemini-3.8-flash-high --dangerously-skip-permissions`（老旧的 `gemini-pro-agent` 已停用）；使用 `agent prompt/read/wait` 派工与跟进，不使用后台批处理包装器或工具白名单。任务范围仍限于指定目录。Windows 使用者须自行配置 Claude Code 的 Gemini 通道；项目不修改全局账号配置。

翻译后执行：

```powershell
python scripts/localization.py build --manifest reports/diff-latest.json --translations data/agent-work/latest/translations.json --output build/LLC_zh-CN
```

合并会检查任务项、原文、当前中文、占位符和换行；拒绝遗漏或重复任务，拒绝已有输出目录。完整复制最新中文包，仅修改校验通过的字段。随后按安装文档加入官方字体。

## 验证

```powershell
python -m unittest discover -s tests -v
```

旧版 `temp_translate.py`、`find_modify_key.py` 等保留作历史代码，当前流程使用 `scripts/localization.py`。

本项目代码保留原许可证；上游译文与字体的授权独立。分发包含上游内容的语言包时，保留来源、署名及其 CC BY-NC-SA 4.0 许可说明，见安装文档。
