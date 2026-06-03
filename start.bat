@echo off
cd /d "%~dp0"

echo ========================================
echo   AI Voice Hub
echo ========================================
echo.
echo Current dir: %cd%
echo.

REM ---- kill old ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING" 2^>nul') do (
    echo Killing old backend PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING" 2^>nul') do (
    echo Killing old frontend PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

REM ---- backend ----
echo.
echo [1] Starting backend on http://localhost:8000
start "Backend" cmd /k "cd /d %~dp0backend && E:\python\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000"

REM ---- frontend ----
echo [2] Starting frontend on http://localhost:5173
if not exist "%~dp0frontend\node_modules" (
    echo Installing frontend deps first...
    cd /d "%~dp0frontend"
    call npm install
    cd /d "%~dp0"
)
start "Frontend" cmd /k "cd /d %~dp0frontend && npx vite --port 5173"

echo.
echo Waiting 5 seconds...
timeout /t 5 /nobreak >nul

echo Opening http://localhost:5173
start http://localhost:5173

echo.
echo If browser does not open, visit http://localhost:5173
pause
