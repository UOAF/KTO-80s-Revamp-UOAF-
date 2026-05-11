@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM === PATH SETUP ===
set "BASE=%~dp0"
set "ROOT=%BASE%.."

:menu
cls
echo ===============================
echo     CAMPAIGN DAY/NIGHT TOOL
echo ===============================
echo.
echo 1 - Campaign Save Day Transition
echo 2 - Campaign Save Night Transition
echo 3 - Change Mission Data And Priority Files Only
echo 4 - Exit
echo.
set /p MODE=Select option: 

if "%MODE%"=="1" set "MODE_NAME=DAY" & set "LIGHT_MODE=--lights-on" & set "MISSION_FOLDER=Day_ops" & set "DO_WIPE=1" & goto select_save
if "%MODE%"=="2" set "MODE_NAME=NIGHT" & set "LIGHT_MODE=--lights-off" & set "MISSION_FOLDER=Night_ops" & set "DO_WIPE=1" & goto select_save
if "%MODE%"=="3" goto mission_only
if "%MODE%"=="4" exit

goto menu


REM ======================================================
REM ================= AVAILABLE SAVES ====================
REM ======================================================

:select_save
cls
echo ============================
echo   AVAILABLE SAVES
echo ============================

set COUNT=0

for %%F in ("%ROOT%\Campaign\*.cam") do (
    set "NAME=%%~nxF"
    set "SKIP=0"

    REM === FILTERS ===
    echo !NAME! | find /I "Auto" >nul && set "SKIP=1"
    echo !NAME! | find /I "instant" >nul && set "SKIP=1"

    REM Match save*.cam specifically (not just any "save")
    echo !NAME! | findstr /I "Save" >nul && set "SKIP=1"

    if "!SKIP!"=="0" (
        set /a COUNT+=1
        set "SAVE[!COUNT!]=!NAME!"
        echo !COUNT! - !NAME!
    )
)

if "!COUNT!"=="0" (
    echo.
    echo ERROR: No valid campaign saves found.
    pause
    goto menu
)

echo.
set /p SAVE_NUM=Select save number: 

call set SAVE_NAME=%%SAVE[%SAVE_NUM%]%%

if "%SAVE_NAME%"=="" (
    echo.
    echo Invalid selection.
    pause
    goto select_save
)

goto process


REM ======================================================
REM =================== PROCESS ==========================
REM ======================================================

:process
cls
echo ============================
echo   PROCESSING %MODE_NAME%
echo ============================

echo.
echo Mode: %MODE_NAME%
echo Save: %SAVE_NAME%
echo Theater: "%ROOT%"
echo.

REM === RUN PYTHON ONLY IF NEEDED ===
if "%DO_WIPE%"=="1" (
    echo Running Night Wiper...
    python "%BASE%night_wiper\night_wiper.py" %LIGHT_MODE% --theater "%ROOT%" "%ROOT%\Campaign\%SAVE_NAME%"
	
    if errorlevel 1 (
        echo.
        echo ERROR: Night Wiper failed.
        pause
        goto menu
    )	 
)

echo.
echo Processing done!

pause

goto apply_mission


REM ======================================================
REM =============== MISSION DATA ONLY ====================
REM ======================================================

:mission_only
cls
echo ============================
echo   MISSION DATA ONLY
echo ============================
echo.
echo 1 - Apply Day mission data
echo 2 - Apply Night mission data
echo 3 - Back
echo.
set /p MDCHOICE=

if "%MDCHOICE%"=="1" set "MODE_NAME=DAY" & set "MISSION_FOLDER=Day_ops" & goto apply_mission
if "%MDCHOICE%"=="2" set "MODE_NAME=NIGHT" & set "MISSION_FOLDER=Night_ops" & goto apply_mission
if "%MDCHOICE%"=="3" goto menu

goto mission_only


REM ======================================================
REM ========== APPLY MISSION DATA AND PRIORITIES =========
REM ======================================================

:apply_mission
echo.
echo Applying %MODE_NAME% Mission Data...

echo Copying from:
echo "%ROOT%\Campaign\%MISSION_FOLDER%\"
echo To:
echo "%ROOT%\Campaign\"

copy /Y "%ROOT%\Campaign\%MISSION_FOLDER%\MissionData_*.xml" "%ROOT%\Campaign\"

echo Applying %MODE_NAME% Mission Priorities...

echo Copying from:
echo "%ROOT%\Campaign\%MISSION_FOLDER%\"
echo To:
echo "%ROOT%\Campaign\"

copy /Y "%ROOT%\Campaign\%MISSION_FOLDER%\*.pri" "%ROOT%\Campaign\"

echo.
echo ============================
echo   DONE
echo ============================

echo NOTE:
echo - Mission data set
echo - Restart BMS to ensure changes take effect
echo.

pause