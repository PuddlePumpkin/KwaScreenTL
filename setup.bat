@echo off
setlocal
set VENV_DIR=C:\GitRepos\KwaScreenTL\.venv

echo Creating virtual environment...
python -m venv "%VENV_DIR%"

echo Installing dependencies...
"%VENV_DIR%\Scripts\pip" install --upgrade pip >nul
"%VENV_DIR%\Scripts\pip" install -r requirements.txt
"%VENV_DIR%\Scripts\pip" install paddleocr

echo Downloading Jamdict database (~120MB, first launch only)...
"%VENV_DIR%\Scripts\python" -c "from jamdict import Jamdict; Jamdict(); print('Jamdict ready.')"

echo.
echo ============================================
echo Setup complete!
echo.
echo To run the app, use:
echo   %VENV_DIR%\Scripts\python main.py
echo ============================================
pause
