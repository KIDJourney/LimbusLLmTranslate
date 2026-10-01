# Windows 自动更新器 (Update-LimbusTranslation)

本更新器用于自动检测 Limbus Company 中文本地化版本的更新，并在有新版本时自动下载安装。仅支持 Windows 系统。

## 使用方法

双击 `Update-LimbusTranslation.bat` 即可启动更新器。

**功能说明：**
- 自动检测：依次通过注册表 Steam 卸载项、Steam libraryfolders.vdf 以及安装目录（检测 LimbusCompany.exe 和 LimbusCompany_Data）来探测游戏路径。
- 若查找到多个合法路径，将提示用户手动选择；若未能检测到游戏路径，将要求用户手动输入。
- 自动检查 `limbus-cn.deadfish.win` 上的最新版本记录。
- 检测到新版本（或本地版本遗失）时：
  - 如果游戏正在运行，会暂停等待其关闭。
  - 下载最新全量包，校验 SHA256 与文件大小。
  - 检查解压安全（ZIP炸弹防护等限制：最大500MB）。
  - 执行 `Install-LimbusTranslation.ps1` 原地更新游戏并自动进行失败恢复。
- 可选 `-Continuous` 参数以支持后台持续检测更新模式。

## 已知未验证限制

1. **环境受限**：本脚本编写环境为 macOS，仅依赖代码层面的结构推导，尚未在真实的 Windows/Steam 环境中运行验证。
2. **下载与校验**：在真实执行时，下载阶段对实际 CDN 大包的速率、防串改、TLS1.3 策略支持，以及 `Expand-Archive` 对跨层级嵌套文件夹提取过程在不同版本的 PowerShell (5.1 vs Core) 中可能存在的细节差异并未进行测试验证。
3. **互斥锁与并发管理**：使用 Local Mutex 进行进程排他锁定机制未经多实例竞态真实测试。
4. **游戏进程阻塞逻辑**：对于可能后台驻留的特殊 Limbus Company 进程，通过 `Get-Process` 判断是否有挂起状态尚未测试。
5. **ZipArchive .NET 程序集加载**: `Add-Type -AssemblyName System.IO.Compression.FileSystem` 及 ZipFile 类在某些精简版或受限 Windows 环境（未完整安装 .NET Framework 4.5+）中的行为尚未实机测试。
