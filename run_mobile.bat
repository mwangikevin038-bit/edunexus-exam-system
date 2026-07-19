@echo off
REM Start EduNexus dev server HIDDEN on all interfaces.
REM No cmd window will pop up. Logs go to %USERPROFILE%\edunexus_*.log.
cd /d "C:\Exam System"
powershell -NoProfile -Command "Start-Process -FilePath 'python' -ArgumentList 'manage.py','runserver','0.0.0.0:8000','--noreload' -WorkingDirectory 'C:\Exam System' -WindowStyle Hidden -RedirectStandardOutput '$env:USERPROFILE\edunexus_out.log' -RedirectStandardError '$env:USERPROFILE\edunexus_err.log'"
echo Server started (hidden). Phone URL: http://192.168.75.230:8000
echo To stop it:  Get-Process python ^| Where-Object { $_.CommandLine -match "runserver" } ^| Stop-Process -Force
