@echo off
setlocal
set VENV_DIR=C:\GitRepos\KwaScreenTL\.venv
set PYTHON=%VENV_DIR%\Scripts\python

echo Creating virtual environment...
python -m venv "%VENV_DIR%"

echo Installing dependencies...
%PYTHON% -m pip install --upgrade pip >nul
%PYTHON% -m pip install -r requirements.txt
%PYTHON% -m pip install paddleocr

echo Attempting optional GPU acceleration (onnxruntime-gpu)...
%PYTHON% -m pip install onnxruntime-gpu 2>nul && (
    echo GPU acceleration enabled.
) || (
    echo GPU acceleration not available ^(no CUDA/compatible GPU found^), using CPU.
)

echo Downloading Jamdict database (~120MB, first launch only)...
%PYTHON% -c "from jamdict import Jamdict; Jamdict(); print('Jamdict ready.')"

echo.
echo ============================================
echo Setup complete!
echo.
echo To run the app, use:
echo   %PYTHON% Src\main.py
echo ============================================
pause
