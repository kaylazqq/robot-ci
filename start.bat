@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist logs mkdir logs

echo.
echo === SWR Push Helper ===
echo.

set "PY="
if exist "%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe" set "PY=%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe"
if not defined PY if exist "%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe" set "PY=%APPDATA%\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY for /f "delims=" %%I in ('where python 2^>nul') do (
  echo %%I | findstr /i "hermes WindowsApps" >nul
  if errorlevel 1 (
    set "PY=%%I"
    goto gotpy
  )
)
:gotpy
if not defined PY (
  echo [ERROR] python.exe not found. Install Python 3 first.
  pause
  exit /b 1
)

echo Using: %PY%
"%PY%" -c "import yaml" >nul 2>&1
if errorlevel 1 "%PY%" -m pip install --disable-pip-version-check -r "%CD%\requirements.txt" -r "%CD%\requirements-huawei.txt"
if errorlevel 1 (
  echo [ERROR] failed to install Python dependencies.
  pause
  exit /b 1
)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*swr-push-helper*server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
ping -n 2 127.0.0.1 >nul

start "swr-push-helper" /MIN cmd /c ""%PY%" -u "%CD%\server.py" > "%CD%\logs\out.txt" 2> "%CD%\logs\err.txt""

set /a n=0
:wait
set /a n+=1
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1/api/health' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }"
if %ERRORLEVEL%==0 goto ok
if %n% GEQ 30 goto fail
ping -n 2 127.0.0.1 >nul
goto wait

:fail
echo [ERROR] start failed. See logs\err.txt
type logs\err.txt 2>nul
pause
exit /b 1

:ok
echo OK  http://127.0.0.1/
start "" "http://127.0.0.1/"
exit /b 0
