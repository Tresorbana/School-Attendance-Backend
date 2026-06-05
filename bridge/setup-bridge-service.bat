@echo off
setlocal enabledelayedexpansion

:: ── Self-elevate ─────────────────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs -Wait"
    exit /b
)

set "TASK_NAME=AttendAIFingerprintBridge"
set "BRIDGE_EXE=%~dp0FingerprintBridge.exe"
set "PS_FILE=%TEMP%\attendai_setup.ps1"

cls
echo.
echo  ============================================================
echo   AttendAI - Fingerprint Scanner One-Time Setup
echo   Run this ONCE. After this the scanner works automatically.
echo  ============================================================
echo.

if not exist "%BRIDGE_EXE%" (
    echo  ERROR: FingerprintBridge.exe not found next to this file.
    pause & exit /b 1
)

:: ── Step 1: Enable WbioSrvc + grant capture permission ───────────────────────
echo  [1/3] Configuring Windows Biometric Service and capture permissions...

sc config WbioSrvc start= auto >nul 2>&1
net start WbioSrvc >nul 2>&1

:: Add current user to WinBiometricGroup (SID S-1-5-32-578).
:: Membership in this group lets non-admin processes call WinBioCaptureSample
:: with WINBIO_DATA_FLAG_INTERMEDIATE — no UAC ever needed after logon.
(
echo try {
echo     Add-LocalGroupMember -SID 'S-1-5-32-578' -Member '%USERNAME%' -ErrorAction Stop
echo     Write-Output 'ADDED'
echo } catch {
echo     if ($_.Exception.Message -like '*already a member*') { Write-Output 'ALREADY_MEMBER' }
echo     else { Write-Output ('FAILED:' + $_.Exception.Message) }
echo }
) > "%PS_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_FILE%" > "%TEMP%\grp_out.txt" 2>&1
set /p GRP_RESULT=<"%TEMP%\grp_out.txt"
del "%PS_FILE%"      >nul 2>&1
del "%TEMP%\grp_out.txt" >nul 2>&1

set "NEED_LOGOFF=0"
if "!GRP_RESULT!"=="ADDED" (
    echo       Added %USERNAME% to fingerprint capture group.
    set "NEED_LOGOFF=1"
) else if "!GRP_RESULT!"=="ALREADY_MEMBER" (
    echo       %USERNAME% already has fingerprint capture permission.
) else (
    echo       Note: !GRP_RESULT!
    echo       You may need to run as Administrator or add %USERNAME% to WinBiometricGroup manually.
)

:: ── Step 2: Remove old Windows Service and stale task ────────────────────────
echo  [2/3] Cleaning up any previous installation...

sc query "AttendAIFingerprint" >nul 2>&1
if !errorlevel! equ 0 (
    sc stop "AttendAIFingerprint" >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc delete "AttendAIFingerprint" >nul 2>&1
    timeout /t 1 /nobreak >nul
)

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: ── Step 3: Register scheduled task ──────────────────────────────────────────
echo  [3/3] Registering fingerprint bridge scheduled task...

(
echo $exe  = '%BRIDGE_EXE%'
echo $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
echo $action    = New-ScheduledTaskAction -Execute $exe -Argument '--pipe-server'
echo $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
echo $principal = New-ScheduledTaskPrincipal -UserId $user -RunLevel Highest -LogonType Interactive
echo $settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([System.TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
echo Register-ScheduledTask -TaskName '%TASK_NAME%' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force ^| Out-Null
echo Write-Host 'Task registered for:' $user
) > "%PS_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_FILE%"
set PSERR=%errorlevel%
del "%PS_FILE%" >nul 2>&1

if %PSERR% neq 0 (
    echo.
    echo  ERROR: Could not register the scheduled task.
    pause & exit /b 1
)

:: Start the task in the current session (may still fail this session if the
:: group token hasn't refreshed yet — that's why the logon trigger also fires)
schtasks /run /tn "%TASK_NAME%" >nul 2>&1

echo.
echo  ============================================================
echo   Setup Complete!
echo.
if "!NEED_LOGOFF!"=="1" (
echo   ONE MORE STEP REQUIRED:
echo.
echo   %USERNAME% was just added to the fingerprint capture group.
echo   Windows requires a fresh logon for this to take effect.
echo.
echo   Please LOG OUT of Windows now and log back in.
echo   The scanner will work automatically after that — forever.
echo   No further setup or admin action will ever be needed.
) else (
echo   The fingerprint bridge will:
echo     - Start automatically every time you log in to Windows
echo     - Connect on demand when the attendance server needs it
echo     - Require NO further admin action ever
echo.
echo   If the scanner still shows an error, log out and back in
echo   once to refresh your Windows session permissions.
echo.
echo   Restart the attendance server now if it is running.
)
echo  ============================================================
echo.
pause
