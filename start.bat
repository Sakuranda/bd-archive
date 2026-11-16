@echo off
setlocal
cd /d "%~dp0" || goto failed

where /q py.exe
if errorlevel 1 goto try_python
py -3 -c "import sys, sqlite3; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto try_python
py -3 archive.py --open %*
if errorlevel 1 goto failed
exit /b 0

:try_python
where /q python.exe
if errorlevel 1 goto need_python
python -c "import sys, sqlite3; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto need_python
python archive.py --open %*
if errorlevel 1 goto failed
exit /b 0

:need_python
echo Install Python 3.10 or later and enable its PATH option, then launch again.
pause
exit /b 1

:failed
echo BD Archive stopped with an error. See the details above.
pause
exit /b 1
