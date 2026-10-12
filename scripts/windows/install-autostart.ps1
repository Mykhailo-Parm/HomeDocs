# Автозапуск HomeDocs при входе в Windows (без окна консоли, с перезапуском при сбое).
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\windows\install-autostart.ps1
# Права администратора не нужны: задача создаётся для текущего пользователя.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "Не найден $pythonw — сначала создай .venv и установи requirements.txt" }

$name = 'HomeDocs'
# --supervise: «сторож» перезапускает сервер через 5 с после любого падения
$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m homedocs --supervise' -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'HomeDocs: домашний архив документов (http://127.0.0.1:8000)' -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Готово: HomeDocs запускается при входе в Windows. Сейчас запущен: http://127.0.0.1:8000"
Write-Host "Лог: $repo\data\homedocs.log"
