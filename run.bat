@echo off
REM Double-click to launch Anonymizer / Redactor from source on Windows.
setlocal
set HERE=%~dp0
set VENV=%USERPROFILE%\.venvs\anonymizer-redactor

if not exist "%VENV%\Scripts\python.exe" (
  echo No environment found. Creating one...
  python -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt"
  "%VENV%\Scripts\python.exe" -m spacy download en_core_web_sm
)

cd /d "%HERE%"
"%VENV%\Scripts\python.exe" -m redactor
endlocal
