@echo off
REM Run on Windows with Python 3.10+ . Needs env: TB_SERVER TB_AGENT_TOKEN TB_ALLOWED_ORIGINS
cd /d %~dp0
python make_config.py || exit /b 1
python -m pip install --quiet pyinstaller || exit /b 1
python -m PyInstaller --onefile --noconsole --clean --name trackobee-setup --add-data "config.json;." ..\agent\trackobee_agent.py || exit /b 1
echo Built: %~dp0dist\trackobee-setup.exe   (optionally sign with signtool)
