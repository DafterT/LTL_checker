@echo off
setlocal
rem Paths for this installation. Changes apply only to iSPIN and its tools.
set "SPIN_DIR=%LOCALAPPDATA%\Programs\Spin"
set "TCL_DIR=C:\ActiveTcl\bin"
set "GCC_DIR=C:\msys64\ucrt64\bin"
set "MSYS_DIR=C:\msys64\usr\bin"
set "DOT_DIR=C:\Program Files\Graphviz\bin"

for %%F in ("%~dp0ispin.tcl" "%SPIN_DIR%\spin.exe" "%TCL_DIR%\wish.exe" "%GCC_DIR%\gcc.exe" "%MSYS_DIR%\rm.exe" "%DOT_DIR%\dot.exe") do (
    if not exist "%%~F" (
        echo ERROR: Required file not found: %%~F
        echo Check the installation paths in this BAT file.
        pause
        exit /b 1
    )
)

set "PATH=%TCL_DIR%;%GCC_DIR%;%MSYS_DIR%;%SPIN_DIR%;%DOT_DIR%;%PATH%"
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot open the iSPIN working directory.
    pause
    exit /b 1
)
start "iSPIN" "%TCL_DIR%\wish.exe" "%~dp0ispin.tcl"
if errorlevel 1 (
    echo ERROR: Could not start iSPIN.
    pause
    exit /b 1
)
endlocal
