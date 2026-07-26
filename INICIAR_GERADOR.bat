@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 vic3_world_generator.py
  goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python vic3_world_generator.py
  goto :end
)
echo Python 3 nao foi encontrado.
echo Instale o Python 3 e marque a opcao Add Python to PATH.
pause
:end
