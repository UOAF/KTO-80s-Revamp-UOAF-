@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set BASE=%~dp0
set ROOT=%BASE%..

:menu
cls
echo ===============================
echo     CAMPAIGN DAY/NIGHT TOOL
echo ===============================
echo.
echo 1 - Day Transition
echo 2 - Night Transition
echo 3 - Exit
echo.
set /p MODE=Select option: 

if "%MODE%"=="1" set MODE_NAME=DAY & set MISSION_FOLDER=Day_ops& goto wipe_option
if "%MODE%"=="2" set MODE_NAME=NIGHT & set MISSION_FOLDER=Night_ops& goto wipe_option
if "%MODE%"=="3" exit

goto menu


:wipe_option
cls
echo ============================
echo   %MODE_NAME% TRANSITION
echo ============================
echo.
echo Wipe ATO?
echo 1 - Yes
echo 2 - No
echo.
set /p WIPE=

if "%WIPE%"=="1" goto backup_option
if "%WIPE%"=="2" goto apply_only

goto wipe_option

:backup_option
cls
echo ============================
echo   BACKUP OPTIONS
echo ============================
echo.
echo Backup and overwrite original save?
echo 1 - Yes
echo 2 - No
echo.
set /p BACKUP=

if "%BACKUP%"=="1" goto select_save
if "%BACKUP%"=="2" goto select_save

goto backup_option


REM ======================================================
REM ========== SAVE SELECTION (NUMBERED LIST) ============
REM ======================================================

:select_save
cls
echo ============================
echo   AVAILABLE SAVES
echo ============================

set COUNT=0

for %%F in ("%ROOT%\Campaign\*.cam") do (
    set /a COUNT+=1
    set "SAVE[!COUNT!]=%%~nxF"
    echo !COUNT! - %%~nxF
)

echo.
set /p SAVE_NUM=Select save number: 

call set SAVE_NAME=%%SAVE[%SAVE_NUM%]%%

if "%SAVE_NAME%"=="" (
    echo Invalid selection.
    pause
    goto select_save
)

goto process


REM ======================================================
REM =============== NO ATO WIPE PATH =====================
REM ======================================================

:apply_only
set SAVE_NAME=

goto process


REM ======================================================
REM =================== PROCESS ==========================
REM ======================================================

:process
cls
echo ============================
echo   PROCESSING %MODE_NAME%
echo ============================

REM === ATO WIPE ONLY IF SELECTED ===

if not "%SAVE_NAME%"=="" (

    echo.
    echo Wiping ATO on: %SAVE_NAME%

    python "%BASE%night_wiper\wipe_ato.py" "%ROOT%\Campaign\%SAVE_NAME%" --campaign-dir "%ROOT%\Campaign"

REM === HANDLE OUTPUT FILE ===
set "WIPED_NAME=!SAVE_NAME:.cam=_wiped.cam!"

if "%BACKUP%"=="1" (
    echo.
    echo Creating backup and overwriting original...

	set "ORIG=%ROOT%\Campaign\!SAVE_NAME!"
	set "WIPED=%ROOT%\Campaign\!WIPED_NAME!"
	set "BAK=%ROOT%\Campaign\!SAVE_NAME:.cam=_bak.cam!"

	move /Y "!ORIG!" "!BAK!"
	move /Y "!WIPED!" "!ORIG!"

    echo Backup created: !SAVE_NAME:.cam=_bak.cam!
    echo Original replaced with wiped version.
) else (
    echo.
    echo Wiped file kept as: !WIPED_NAME!
)
)

pause

REM === MISSION DATA SWAP ===
echo.
echo Applying %MODE_NAME% mission data...

echo Copying from:
echo "%ROOT%\Campaign\%MISSION_FOLDER%\MissionData_*.xml"
echo To:
echo "%ROOT%\Campaign\"

copy /Y "%ROOT%\Campaign\%MISSION_FOLDER%\MissionData_*.xml" "%ROOT%\Campaign\"

echo.
echo ============================
echo   DONE
echo ============================

pause