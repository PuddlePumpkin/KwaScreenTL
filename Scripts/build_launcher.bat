@echo off
setlocal
set PROJ_DIR=%~dp0..

echo Closing running instances...
taskkill /F /IM Launcher.exe /T >nul 2>&1
powershell -Command "Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*KwaScreenTL*' } | Stop-Process -Force" >nul 2>&1

echo Converting icon...
"%PROJ_DIR%\.venv\Scripts\python.exe" "%PROJ_DIR%\Scripts\convert_icon.py" 2>&1

echo Building launcher...
dotnet publish "%PROJ_DIR%\Launcher\Launcher.csproj" -c Release --nologo -o "%PROJ_DIR%\Launcher\bin\published" 2>&1
if %ERRORLEVEL% neq 0 (
    echo Build failed.
    exit /b %ERRORLEVEL%
)

if exist "%PROJ_DIR%\Launcher\AppIcon.ico" (
    copy /Y "%PROJ_DIR%\Launcher\AppIcon.ico" "%PROJ_DIR%\Launcher\bin\published\" >nul
    echo [icon] Copied to publish output
)

echo Done: %PROJ_DIR%\Launcher\bin\published\Launcher.exe