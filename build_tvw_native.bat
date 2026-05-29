@echo off
setlocal
set "PATH=C:\msys64\ucrt64\bin;%PATH%"
cd /d "%~dp0"
gcc -O3 -shared -static-libgcc -Wl,--strip-all -o tvw_native.dll tvw_native.c
exit /b %errorlevel%
