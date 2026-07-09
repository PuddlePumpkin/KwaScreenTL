@echo off
echo ===============================================================================
echo   Step 1/2: Converting Sanseido Kokugo Jiten (sankokudict.db)
echo ===============================================================================
python "%~dp0Src\convert_dict.py"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Step 1 failed with exit code %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ===============================================================================
echo   Step 2/2: Converting Kanken Kanji Jiten (kankidict.db)
echo ===============================================================================
python "%~dp0Src\convert_kanjidict.py"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Step 2 failed with exit code %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ===============================================================================
echo   Both dictionaries converted successfully!
echo   sankokudict.db + kankidict.db
echo ===============================================================================
pause
