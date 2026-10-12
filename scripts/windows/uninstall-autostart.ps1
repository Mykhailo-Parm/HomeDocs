# Убрать автозапуск HomeDocs и остановить сервер, запущенный задачей.
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall-autostart.ps1
$name = 'HomeDocs'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    # сторож и сервер, запущенные задачей
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -match '-m homedocs' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'Автозапуск HomeDocs удалён.'
} else {
    Write-Host 'Автозапуск HomeDocs не был настроен.'
}
