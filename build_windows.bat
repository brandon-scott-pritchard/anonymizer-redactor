@echo off
REM Build Anonymizer-Redactor.exe for Windows.
REM Must be run ON a Windows machine - a .exe cannot be produced from macOS.
setlocal

set HERE=%~dp0
set STAGE=%TEMP%\anonymizer-build
set BUILD_VENV=%STAGE%\venv

echo ==^> Staging sources in %STAGE%
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%\src"
xcopy /e /i /q "%HERE%redactor" "%STAGE%\src\redactor" >nul
copy /y "%HERE%requirements.txt" "%STAGE%\src\" >nul
copy /y "%HERE%build\launcher.py" "%STAGE%\src\" >nul
copy /y "%HERE%build\redactor.spec" "%STAGE%\src\" >nul

echo ==^> Creating build environment
python -m venv "%BUILD_VENV%"
"%BUILD_VENV%\Scripts\python.exe" -m pip install --upgrade pip wheel
"%BUILD_VENV%\Scripts\python.exe" -m pip install -r "%STAGE%\src\requirements.txt" pyinstaller
"%BUILD_VENV%\Scripts\python.exe" -m spacy download en_core_web_sm

echo ==^> Building
cd /d "%STAGE%\src"
"%BUILD_VENV%\Scripts\pyinstaller.exe" --noconfirm --clean redactor.spec

echo ==^> Copying the build back
if exist "%HERE%dist\Anonymizer-Redactor" rmdir /s /q "%HERE%dist\Anonymizer-Redactor"
if not exist "%HERE%dist" mkdir "%HERE%dist"
xcopy /e /i /q "%STAGE%\src\dist\Anonymizer-Redactor" "%HERE%dist\Anonymizer-Redactor" >nul

echo.
echo Built: dist\Anonymizer-Redactor\Anonymizer-Redactor.exe
echo.
echo OCR still needs the Tesseract binary installed on the machine:
echo   https://github.com/UB-Mannheim/tesseract/wiki
endlocal
