@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0run.py" & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0run.py" & exit /b)
py "%~dp0run.py"
