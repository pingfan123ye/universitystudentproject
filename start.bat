@echo off
title AI Voice Hub
cd /d "%~dp0"

echo ========================================
echo   AI Voice Hub - Starting...
echo ========================================
echo.

echo [0/5] Cleaning old processes...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>^&1') do (
    taskkill /F /PID %%a 2>nul
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING" 2^>^&1') do (
    taskkill /F /PID %%a 2>nul
)
echo   Ports cleared
echo.

echo [1/5] Checking Ollama...
ollama list | findstr "NAME"
if %errorlevel% neq 0 (
    echo   Starting Ollama...
    start "" "ollama serve"
    echo   Waiting 5s for Ollama...
    timeout /t 5 /nobreak
) else (
    echo   Ollama is running
)

echo [2/5] Checking model...
ollama list | findstr "qwen2.5"
if %errorlevel% neq 0 (
    echo   Pulling qwen2.5:7b, please wait...
    ollama pull qwen2.5:7b
    echo   Done. Please re-run this script.
    pause
    exit /b
) else (
    echo   Qwen2.5:7B ready
)

echo [3/5] Checking frontend deps...
if not exist "%~dp0frontend\node_modules" (
    echo   Installing...
    cd frontend
    call npm install
    cd ..
)

echo [4/5] Starting backend on port 8000...
start "Backend" cmd /k "cd /d %~dp0backend && E:\python\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000"

echo [5/5] Starting frontend on port 5173...
start "Frontend" cmd /k "cd /d %~dp0frontend && npx vite --port 5173"

echo.
echo   Backend  : http://localhost:8000
echo   Frontend : http://localhost:5173
echo   Waiting 8s for services...
timeout /t 8 /nobreak

echo   Opening browser...
start http://localhost:5173

echo.
echo If browser does not open, visit http://localhost:5173
pause
