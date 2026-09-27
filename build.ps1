# BlueMouse 打包脚本: 生成 dist\BlueMouse\ 目录版应用
# 用法: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m PyInstaller --noconfirm --clean --onedir --windowed --name BlueMouse `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all sounddevice `
  --collect-all pynput --collect-all customtkinter --collect-all pystray `
  --collect-all PIL --add-data "app\assets;assets" `
  app\bluemouse_app.py
if ($LASTEXITCODE -ne 0) {
    # PS5.1 不会因原生命令失败而中断, 必须显式检查, 否则会假报打包成功
    Write-Error "PyInstaller 打包失败 (exit=$LASTEXITCODE)"
    exit $LASTEXITCODE
}
Write-Host "`n打包完成: dist\BlueMouse\BlueMouse.exe"
Write-Host "接着运行: powershell -ExecutionPolicy Bypass -File install.ps1"
