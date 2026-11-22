@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
pushd "%~dp0" || exit /b 1
title Crysis Save Editor - Auto Setup Launcher

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "VENV_PYW=%CD%\.venv\Scripts\pythonw.exe"

if not exist "crysis_save_editor.py" (
    echo [ERROR] crysis_save_editor.py was not found next to this launcher.
    goto :fatal
)

if not exist "%VENV_PY%" (
    echo [INFO] Checking Python...
    set "BOOTSTRAP_PY="
    py -3 -c "import sys; print(sys.version)" >nul 2>&1
    if not errorlevel 1 set "BOOTSTRAP_PY=py -3"
    if not defined BOOTSTRAP_PY (
        python -c "import sys; print(sys.version)" >nul 2>&1
        if not errorlevel 1 set "BOOTSTRAP_PY=python"
    )
    if not defined BOOTSTRAP_PY (
        echo [ERROR] Python 3 is not installed or is not available on PATH.
        echo Install Python 3 from https://www.python.org/downloads/windows/
        goto :fatal
    )
    echo [INFO] Creating isolated .venv environment...
    !BOOTSTRAP_PY! -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Cannot create the virtual environment.
        goto :fatal
    )
)

"%VENV_PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Tkinter is missing from this Python installation.
    echo Install a Python distribution with Tk/Tcl support.
    goto :fatal
)

"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing pip...
    "%VENV_PY%" -m ensurepip --upgrade
    if errorlevel 1 (
        echo [ERROR] pip installation failed.
        goto :fatal
    )
)

if exist "requirements.txt" (
    echo [INFO] Checking project dependencies...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r "requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Project dependencies could not be installed.
        goto :fatal
    )
)

"%VENV_PY%" -c "from tkinterdnd2 import TkinterDnD" >nul 2>&1
if errorlevel 1 (
    echo [INFO] tkinterdnd2 / TkDND is missing. Installing automatically...
    "%VENV_PY%" -m pip install --disable-pip-version-check tkinterdnd2
    if errorlevel 1 (
        echo [WARNING] Drag-and-drop could not be installed.
        echo [WARNING] The editor will start without this optional component.
    )
)

if not exist "%VENV_PYW%" (
    echo [ERROR] pythonw.exe was not found in the virtual environment.
    goto :fatal
)
echo [INFO] Starting Crysis Save Editor (console will close)...
start "" /D "%CD%" "%VENV_PYW%" "%CD%\crysis_save_editor.py" %*
if errorlevel 1 (
    echo [ERROR] Cannot start the editor.
    goto :fatal
)
popd
exit /b 0

:fatal
echo.
echo [ERROR] Setup was not completed.
pause
popd
exit /b 1
