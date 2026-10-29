@echo off
rem Blockfield download speed test: the same 32 MiB slice from every candidate host, two passes.
rem ASCII only, so no code page switch is needed. Output format matches blockfield-speedtest.sh.
setlocal
set "SLICE=0-33554431"
rem Gcore test resource such as https://cdn-test.blockfield.pro. Leave empty to skip it.
set "GCORE=https://cdn-test.blockfield.pro"
set "HOME_URL=https://blockfield.pro/files/mods/blockfield-music-6.jar"
set "R2_URL=https://assets.blockfield.pro/music/music-v6/blockfield-music-6.jar"
set "GITHUB_URL=https://github.com/Blockfield/blockfield-releases/releases/download/v2.71.0/Blockfield-client.zip"
set "OUT=%~dp0blockfield-speedtest.txt"

where curl >nul 2>nul || (
    echo curl.exe was not found: Windows 10 version 1803 or newer is required.
    pause
    exit /b 1
)
echo Blockfield download speed test.
echo Turn off VPN and stop other downloads first.
echo It downloads up to 270 MB and takes up to 6 minutes.
echo.
choice /c 12345 /n /m "Country: 1 Russia, 2 Ukraine, 3 Belarus, 4 Kazakhstan, 5 other "
for /f "tokens=%errorlevel%" %%c in ("RU UA BY KZ other") do set "COUNTRY=%%c"
choice /c 12 /n /m "Connection: 1 home internet, 2 mobile "
for /f "tokens=%errorlevel%" %%c in ("home mobile") do set "CONN=%%c"
choice /c 12 /n /m "VPN or proxy on right now? 1 no, 2 yes "
for /f "tokens=%errorlevel%" %%c in ("no yes") do set "VPN=%%c"
set "ISP="
set /p "ISP=Internet provider, for example Rostelecom or Kyivstar: "
if not defined ISP set "ISP=unknown"
set "STARTED=%date% %time%"
rem Delayed expansion only: special characters in the typed name or the folder path stay text.
setlocal EnableDelayedExpansion
> "!OUT!" echo BF1;meta;country=!COUNTRY!;conn=!CONN!;vpn=!VPN!;isp=!ISP!;time=!STARTED!;os=windows
endlocal

rem The second pass runs in reverse order and finds the CDN caches warm.
call :measure home 1 "%HOME_URL%"
call :measure r2 1 "%R2_URL%"
call :measure github 1 "%GITHUB_URL%"
if defined GCORE call :measure gcore 1 "%GCORE%/files/mods/blockfield-music-6.jar"
if defined GCORE call :measure gcore 2 "%GCORE%/files/mods/blockfield-music-6.jar"
call :measure github 2 "%GITHUB_URL%"
call :measure r2 2 "%R2_URL%"
call :measure home 2 "%HOME_URL%"

type "%OUT%" | clip
echo.
echo Done. The result is saved in "%OUT%" and copied: paste it into Discord.
pause
exit /b 0

:measure
echo Testing %1, pass %2 ...
rem No revocation check: an unreachable CRL server would fail one candidate for a reason players never see.
curl -s -L --ssl-no-revoke -r %SLICE% -o NUL --connect-timeout 15 --max-time 45 -w "BF1;%1;%2;%%{http_code};%%{size_download};%%{speed_download};%%{time_connect};%%{time_starttransfer};%%{time_total};%%{remote_ip};" %3 >> "%OUT%"
rem The redirection goes first, otherwise a trailing digit of the code reads as a handle number.
>> "%OUT%" echo exit=%errorlevel%
exit /b 0
