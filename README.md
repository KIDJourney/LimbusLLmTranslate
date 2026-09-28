# LimbusLLmTranslate

面向 Windows 版《边狱公司》的中文补译工具。使用游戏自带的自定义语言接口，安装到 `LimbusCompany_Data\Lang\LLC_zh-CN`。

- [Windows 安装、回滚与来源](docs/windows-install.md)
- [9 月原文更新与修正结果](reports/20260928-september.md)；早期 5 条补译包已撤回。
- `scripts/localization.py`：拉取快照、字段级 diff、准备 Agent 输入、校验并合并翻译。Python 3.9+，仅标准库；默认更新只需网络。
- `scripts/Install-LimbusTranslation.ps1`：Windows PowerShell 5.1+ 安装脚本。

## 更新与 diff

在项目根目录执行。每次使用新快照目录，脚本拒绝覆盖已有快照。默认更新不改 Git 子模块。

```powershell
python scripts/localization.py update --output data/snapshots/latest
python scripts/localization.py diff --source data/snapshots/latest/KR --chinese data/snapshots/latest/LLC_zh-CN --output reports/diff-latest.json
```

更新先从公开版本服务查询资源版本，再从游戏官方 CDN 下载韩文 `localize_kr.zip`，从 LLC 最新 Release 下载中文。记录版本、发现来源、时间、URL 和 SHA-256；按目录保留文件，只去掉原文文件名的 `KR_` 前缀。若版本服务不可用，采用公开发布记录中的最新资源版本，并在 provenance 中明确标记；不会静默退回旧 Git 原文。

Git `KR` 已停在 2026-08-06，不能用于判断 9 月更新。只在历史对照时显式使用 `update --source-kind git`；此模式需要 Git，并拒绝更新有本地改动的子模块。当前默认发现的版本仍需与目标 Windows 游戏版本相符，才能完成实机验收。

如有上一版原文，加上 `--previous-source <上一版KR目录>`，把原文变更列入复核清单。中文和韩文内容不同本来就是翻译结果，不会因此被标成待翻译。

匹配使用相对路径、带类型的 ID、重复 ID 出现次序和嵌套字段；没有 ID 的数组按索引匹配，需要关注上游数组重排。`TEXT_KEYS` 覆盖剧情、技能、flavor、RPG 对话、任务目标和字符串数组；未知韩文字段会进入复核，不静默遗漏。ID/model 不翻译；中韩混合术语、演出注释、空白、内部标签和占位文本进入 `review`。这些规则属于保守筛选，仍需人工审校。

## 交给 Claude Code / Gemini

```powershell
python scripts/localization.py prepare-agent --manifest reports/diff-latest.json --output data/agent-work/latest
```

任务目录含 `input.json`、术语表和 `task.txt`。用已配置 Gemini 的 Claude Code 执行该任务，只写 `translations.json` 和 `notes.md`。不得启动其他 Codex/GPT Agent，不得回退到 Claude 模型；凭据通过现有配置读取，不写入仓库。

家庭委派使用 Herdr 右侧交互式 Claude Code，在已配置 Gemini token 通道的环境中以 `agent start --kind claude` 启动，显式传入 `--model gemini-account/gemini-pro-agent --dangerously-skip-permissions`；使用 `agent prompt/read/wait` 派工与跟进，不使用后台批处理包装器或工具白名单。任务范围仍限于指定目录。Windows 使用者须自行配置 Claude Code 的 Gemini 通道；项目不修改全局账号配置。

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
