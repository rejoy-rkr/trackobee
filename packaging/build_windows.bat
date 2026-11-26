@echo off
REM Run on Windows with Python 3.10+. Produces a GENERIC agent (no secrets baked in): packaging\dist\trackobee-setup.exe
REM Copy it to the server's installers\ folder. Per-organization settings arrive via the generated setup script (org.json).
cd /d %~dp0
python -m pip install --quiet pyinstaller || exit /b 1
python -m PyInstaller --onefile --noconsole --clean --name trackobee-setup ..\agent\trackobee_agent.py || exit /b 1
echo Built: %~dp0dist\trackobee-setup.exe   (sign it with signtool to avoid SmartScreen warnings)
