# Windows 安装与回滚

汉化通过游戏自带的自定义语言接口加载。把文本和字体放入 `LimbusCompany_Data\Lang\LLC_zh-CN`，在启动页面选择该语言即可，无需注入 DLL。

当前草稿包为 `LimbusLLmTranslate-20260928-september.zip`，依据 9 月 24 日原文生成。早期 `20260928-draft.zip` 已撤回并移入 `build/withdrawn`，不要安装。ZIP 解压后包含 `build/LLC_zh-CN`、`scripts` 和本说明，以下命令可在解压目录执行。

## 准备语言包

1. 下载本项目生成的完整语言目录，或上游最新 Release 的 `LimbusLocalize_*.zip`。不要把 GitHub 的 Source code 压缩包当成安装包。
2. 从上游 `Fonts/LLCCN-Font.7z` 取得字体，解压后将其中的 `Font` 文件夹合并到语言目录。
3. 准备好的目录应为：

```text
LLC_zh-CN/
  MainUIText.json
  ...其他 JSON 和剧情子目录...
  Font/
    Context/
      ChineseFont.ttf
    Title/
```

2026-09-28 实际检查的上游字体压缩包中，`Title` 是空目录；保留该目录即可。`Context` 必须有非空 TTF/OTF 文件。自制语言包应保留完整文本树，不能只拿七条补译 JSON 直接安装。

## 安装

关闭游戏。在 Steam 库中右击游戏，选择“管理 → 浏览本地文件”，取得包含 `LimbusCompany.exe` 的目录。

在项目根目录打开 Windows PowerShell 5.1 或更新版本，先预览：

```powershell
.\scripts\Install-LimbusTranslation.ps1 -GamePath 'D:\SteamLibrary\steamapps\common\Limbus Company' -PackagePath '.\build\LLC_zh-CN' -WhatIf
```

确认显示的目标路径正确，去掉 `-WhatIf` 执行安装：

```powershell
.\scripts\Install-LimbusTranslation.ps1 -GamePath 'D:\SteamLibrary\steamapps\common\Limbus Company' -PackagePath '.\build\LLC_zh-CN'
```

若本机执行策略阻止已审阅的脚本，可仅对这一次进程使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-LimbusTranslation.ps1 -GamePath 'D:\SteamLibrary\steamapps\common\Limbus Company' -PackagePath '.\build\LLC_zh-CN'
```

脚本在写入前检查游戏进程、游戏目录、JSON、字体和路径；拒绝源目标重叠以及符号链接/目录联接。先复制到临时目录，逐文件核对 SHA-256，再备份并替换指定语言。备份放在游戏根目录的 `LimbusTranslationBackups`，不会作为额外语言出现在游戏列表中。失败时尝试恢复旧目录。脚本不要求管理员权限，目标目录需对当前用户可写。

启动游戏，在进入游戏前的主页面点击自定义语言按钮，选择 `LLC_zh-CN`；将基础语言设为 English，避免日文基础语言引起字体不一致。首次进入后检查主菜单、剧情和战斗提示，确认中文和字体正常。

## 回滚或停用

关闭游戏，将当前 `LimbusCompany_Data\Lang\LLC_zh-CN` 移到游戏外保存，再把安装日志给出的备份目录移回该位置，目录名恢复成 `LLC_zh-CN`。

只想停用时，在游戏的自定义语言选项取消选择即可。上游工具箱更新可能覆盖本项目补译，更新后需重新生成并安装。

## 来源与验证边界

核验日期：2026-09-28。

- [LLC 上游手动安装说明](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/blob/main/.github/readme/manual-install.md)：文本、字体、语言选择与 English 设置。
- [本次文本 Release 2026092801](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/releases/tag/2026092801)：实际 ZIP 内路径为 `LimbusCompany_Data/Lang/LLC_zh-CN/`。
- [上游字体文件](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/blob/main/Fonts/LLCCN-Font.7z)。
- [上游许可证](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany/blob/main/LICENSE)：上游语言资料使用 CC BY-NC-SA 4.0，分发衍生包须署名、非商业使用并以相同方式共享。本项目补译是独立草稿，不代表 LLC 官方发布。

当前开发机不是 Windows，没有运行 Windows 安装脚本或启动游戏验证。Python 测试、真实 ZIP 布局和文件校验结果见本次报告；不能替代游戏内验收。
