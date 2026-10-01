@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-LimbusTranslation.ps1" %*
set PS_EXIT_CODE=%ERRORLEVEL%
if %PS_EXIT_CODE% neq 0 (
    echo Update failed with error code %PS_EXIT_CODE%.
    pause
)
exit /b %PS_EXIT_CODE%
endlocal
