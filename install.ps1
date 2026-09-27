# BlueMouse 安装脚本: 复制到用户目录 + 创建快捷方式 + (可选)开机自启
# 用法: powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "dist\BlueMouse"
if (-not (Test-Path (Join-Path $src "BlueMouse.exe"))) {
    Write-Error "找不到 dist\BlueMouse\BlueMouse.exe, 请先运行 build.ps1"
}
$dst = Join-Path $env:LOCALAPPDATA "BlueMouse"
Get-Process BlueMouse -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\*" $dst -Recurse -Force

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:USERPROFILE\Desktop\BlueMouse 遥控中心.lnk")
$lnk.TargetPath = Join-Path $dst "BlueMouse.exe"
$lnk.WorkingDirectory = $dst
$lnk.Save()
$sm = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$lnk2 = $ws.CreateShortcut("$sm\BlueMouse 遥控中心.lnk")
$lnk2.TargetPath = Join-Path $dst "BlueMouse.exe"
$lnk2.WorkingDirectory = $dst
$lnk2.Save()

# 开机自启(应用内开关也管理同一个注册表项)
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
  -Name "BlueMouse" -Value (Join-Path $dst "BlueMouse.exe")

Write-Host "已安装到 $dst"
Write-Host "已创建桌面/开始菜单快捷方式, 并设置开机自启"
Write-Host "卸载: powershell -ExecutionPolicy Bypass -File uninstall.ps1"
Start-Process (Join-Path $dst "BlueMouse.exe")
