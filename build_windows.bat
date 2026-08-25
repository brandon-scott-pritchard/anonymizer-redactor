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
xcopy /e /i /q "%HERE%webapp" "%STAGE%\src\webapp" >nul
copy /y "%HERE%requirements.txt" "%STAGE%\src\" >nul
copy /y "%HERE%build\launcher.py" "%STAGE%\src\" >nul
copy /y "%HERE%build\redactor.spec" "%STAGE%\src\" >nul
REM The vendored Tesseract tarball, if vendor_tesseract_windows.py was run.
REM Without it the exe still has OCR - RapidOCR is bundled from pip.
if exist "%HERE%vendor\tesseract-windows-*.tar.gz" (
    mkdir "%STAGE%\src\vendor"
    copy /y "%HERE%vendor\tesseract-windows-*.tar.gz" "%STAGE%\src\vendor\" >nul
)

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
echo OCR is built in: RapidOCR ships inside the exe, so scanned PDFs work
echo with no further setup. To bundle the preferred Tesseract engine too,
echo install it from https://github.com/UB-Mannheim/tesseract/wiki, run
echo   python vendor_tesseract_windows.py
echo and build again.
endlocal
