@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   Building single-file exes with Nuitka
echo ============================================
echo.

if not exist dist mkdir dist

echo [1/2] Building ipdown.exe ...
uv run --python 3.13 -m nuitka ^
    --onefile ^
    --standalone ^
    --output-dir=dist ^
    --output-filename=ipdown.exe ^
    --assume-yes-for-downloads ^
    --company-name="MiMoCode" ^
    --product-name="IP Down" ^
    --product-version="1.0.0" ^
    ipdown.py

if %ERRORLEVEL% neq 0 (
    echo [FAIL] ipdown build failed!
    pause
    exit /b 1
)
echo [OK] ipdown.exe
echo.

echo [2/2] Building sangfor_auth.exe ...
uv run --python 3.13 -m nuitka ^
    --onefile ^
    --standalone ^
    --output-dir=dist ^
    --output-filename=sangfor_auth.exe ^
    --assume-yes-for-downloads ^
    --include-package=httpx ^
    --include-package=anyio ^
    --include-package=sniffio ^
    --include-package=certifi ^
    --include-package=httpcore ^
    --company-name="MiMoCode" ^
    --product-name="Sangfor Auth" ^
    --product-version="1.0.0" ^
    sangfor_auth.py

if %ERRORLEVEL% neq 0 (
    echo [FAIL] sangfor_auth build failed!
    pause
    exit /b 1
)
echo [OK] sangfor_auth.exe
echo.

echo ============================================
echo   Build complete!
echo ============================================
echo.
if exist "dist\ipdown.exe" (
    for %%A in (dist\ipdown.exe) do echo   ipdown.exe             %%~zA bytes
)
if exist "dist\sangfor_auth.exe" (
    for %%A in (dist\sangfor_auth.exe) do echo   sangfor_auth.exe       %%~zA bytes
)
echo.
pause
