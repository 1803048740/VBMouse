# BlueMouse 卸载脚本
$dst = Join-Path $env:LOCALAPPDATA "BlueMouse"
Get-Process BlueMouse -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
  -Name "BlueMouse" -ErrorAction SilentlyContinue
Remove-Item "$env:USERPROFILE\Desktop\BlueMouse 遥控中心.lnk" -ErrorAction SilentlyContinue
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\BlueMouse 遥控中心.lnk" -ErrorAction SilentlyContinue
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Write-Host "BlueMouse 已卸载 (识别模型缓存保留在 HuggingFace 缓存目录, 如需彻底清理请手动删除)"
