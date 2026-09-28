# 9 月原文补取与修正报告

已取得官方 CDN 的 9 月 24 日韩文原文，并与 LLC 9 月 28 日中文发布包重新比较。此前只更新中文 Release、仍用 8 月 Git 原文比较的流程不完整；早期 5 条补译包已撤回。

## 数据与差异

| 项目 | 实际结果 |
| --- | --- |
| 原文资源版本 | `l20260924_4eb8-Rb7MrVjKfF17k-j` |
| 原文来源 | 游戏官方 `downloadcommon.limbuscompanycdn.org` |
| 原文 JSON | 2,277 个，均解析成功 |
| 中文 Release | `2026092801`，2,237 个 JSON |
| 相比 8 月 6 日原文 | 新增 203 个文件、修改 56 个文件、删除 0 个文件；按 JSON 内容比较，忽略格式变化 |
| 新增文件中的韩文文本字段 | 20,123 个，包含剧情、RPG 对话、任务目标、描述等；不是待翻译数量 |
| 实际待补译 | 25 个字段，涉及 6 个文件 |

本次完整更新命令成功访问版本查询服务，返回 9 月 24 日版本。此前一次直接请求收到 403，曾用公开 Release 的版本记录发现同一个版本；后续成功请求及官方 CDN 下载结果均已保存。未连接用户 Windows 游戏，因此没有声称已独立核对该游戏的安装版本。

来源：

- [官方 CDN 韩文资源包](https://downloadcommon.limbuscompanycdn.org/l20260924_4eb8-Rb7MrVjKfF17k-j/Assets/LocalizePatch/localize_kr.zip)
- [公开版本查询服务](https://limbus.lcta.top/api/status)
- [公开资源版本记录](https://github.com/HZBHZB1234/LCTA_auto_update/releases/tag/2026092401)
- [LLC 中文 Release](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/2026092801)
- [本次来源与 SHA-256](september-provenance.json)、[原文文件差异](september-source-delta.json)

9 月的更新规模很大，但 9 月 28 日中文发布包已覆盖其中大部分内容，所以剩余补译数量不等于游戏更新量。

## 旧产物撤回

旧包新增的 `battle_s3_11005_1_2-03`、`select_all`、`deselect_all`、`story_voice_setting_ui_title`、`CantBreak`，在 9 月原文中全部不存在。旧比较把已删除的原文条目误当成新版中文漏译。旧语言目录和 ZIP 已保留到 `build/withdrawn`，不再作为当前产物。

新的完整包从原始 9 月 28 日中文包重新构建，已确认没有补入这 5 个 ID。

## 25 条补译与 9 条审校

- `CouponUIText.json`：15 条会员代码界面文本。
- `BattleKeywords-a1c10p2.json`、`Bufs-a1c10p2.json`：各 1 条“单脚人 B路线 强化”。角色名与最新中文 `Enemies-a1c10p1.json` 的 ID 1483 一致。
- `PanicInfo-a1c10p1.json`：4 条描述。
- `PanicInfo-a1c10p2.json`：2 条描述。
- `Skills_Abnormality-a1c10p2.json`：2 条描述。

原文变更的 9 条另行审校。主控决定全部保留现有中文：点穴替换说明原本就存在，震颤“层数”表达次数而非强度，其余建议属于措辞或排版差异，不构成本次必须更新的内容。没有直接采用 Agent 的所有修改建议。

其余 510 条是内部或占位文本 468 条、中韩混合术语 42 条，按保守规则留在复核清单，未修改。合并后自动候选为 0；这不等于这些复核项已全部完成游戏内人工确认。

- [完整 diff](diff-september-current.json)
- [最终译文](../translations/september-20260924.json)
- [9 条审校与主控决定](september-review-decisions.json)
- [实际 25 处修改](september-package-changes.json)
- [合并后 diff](post-build-september.json)

## 流程修正

`update` 默认改为“查询资源版本 → 官方 CDN 韩文 ZIP → 最新中文 Release”，不再默认使用过期 Git 原文。查询服务不可用时，尝试公开发布记录，并明确记录回退来源；不会悄悄使用旧 Git 数据。下载在临时目录完成校验后再形成快照。

原文 ZIP 只移除文件名的 `KR_` 前缀，保留目录层级；校验 JSON、重复路径和目录穿越。中文 ZIP 校验 GitHub 提供的 SHA-256。

diff 已补齐 `flavor`、RPG 对话、任务目标、显示名、结果字符串数组等字段。未来未识别的韩文字段会明确列入复核，避免字段名单过窄造成静默漏算。

## Agent 与验证

使用右侧交互式 Claude Code，Herdr 会话 `agent-control`，pane `w8:p5`，名称 `limbus-september`。显式模型参数为 `gemini-account/gemini-pro-agent`，响应记录模型名为 `gemini-pro-default`。启动包含用户授权的 `--dangerously-skip-permissions`，没有 dontAsk、工具白名单或批处理包装器。

Claude 会话 ID：`25beb919-990f-4a2c-853b-0372d4deb114`。Agent 输出 17 条译文、8 条补充译文和 9 条审校决定；没有权限拒绝。曾搜索一个不存在的 EN 目录，命令失败后继续使用韩文和中文上下文，未影响输出。

主控完成的验证：

- `python3 -m unittest discover -s tests -v`：13 项通过。
- 实际默认更新命令下载成功，韩文和中文 ZIP 校验通过。
- 逐个 JSON 基础值比较：只改变 25 个文本字段，分布在 6 个文件；没有删除键，也没有修改数字、ID 或 model。
- 25 条翻译的任务键、原文、当前中文、占位符及换行校验通过；上游资料未被翻译任务修改。
- 新包没有旧版误补的 5 个 ID，ZIP CRC 校验通过。

当前完整目录：`build/LLC_zh-CN`。当前 ZIP：`build/LimbusLLmTranslate-20260928-september.zip`，含字体、许可、安装脚本与说明。

Windows PowerShell 安装、回滚和游戏内显示仍未实机验证。
