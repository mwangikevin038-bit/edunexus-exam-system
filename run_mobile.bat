@echo off
REM Start EduNexus dev server on all interfaces so a phone on the same Wi-Fi can reach it.
cd /d "C:\Exam System"
echo Starting EduNexus on 0.0.0.0:8000 ...
echo Phone URL: http://192.168.75.230:8000
echo Close this window to stop the server.
echo.
python manage.py runserver 0.0.0.0:8000
pause
