@echo off
setlocal enabledelayedexpansion
echo =====================================================
echo  AttendAI Fingerprint Scanner - One-Time Admin Setup
echo  Run this script ONCE as Administrator.
echo =====================================================
echo.

:: Enable and start Windows Biometric Service
sc config WbioSrvc start= auto
if !ERRORLEVEL! NEQ 0 (
    echo ERROR: Could not configure WbioSrvc. Are you running as Administrator?
    pause
    exit /b 1
)

net start WbioSrvc
if !ERRORLEVEL! NEQ 0 (
    :: "already started" is fine — verify the service is actually running
    sc query WbioSrvc | findstr /I "RUNNING" >nul
    if !ERRORLEVEL! NEQ 0 (
        echo ERROR: Could not start WbioSrvc.
        pause
        exit /b 1
    )
    echo WbioSrvc is already running.
)

echo.
echo [1/2] Windows Biometric Service configured and running.

:: Grant the current user permission to call WinBioCaptureSample.
:: WinBiometricGroup SID S-1-5-32-578 allows non-admin raw/intermediate capture.
:: We try by name first, then fall back to SID via PowerShell (handles localized names).
echo [2/2] Adding %USERNAME% to WinBiometricGroup (SID S-1-5-32-578)...

net localgroup WinBiometricGroup %USERNAME% /add >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo      Added via net localgroup. LOG OUT and back in for the change to take effect.
    goto :setup_done
)

:: Check if the net localgroup failure was "already a member" (error 1378)
net localgroup WinBiometricGroup %USERNAME% /add 2>&1 | findstr /I "1378" >nul
if !ERRORLEVEL! EQU 0 (
    echo      %USERNAME% is already in WinBiometricGroup.
    goto :setup_done
)

:: Fall back to PowerShell using the stable SID (handles localized group names)
echo      net localgroup failed; trying PowerShell with SID S-1-5-32-578...
powershell -NoProfile -Command ^
  "try { Add-LocalGroupMember -SID 'S-1-5-32-578' -Member '%USERNAME%' -ErrorAction Stop; Write-Host '     Added via PowerShell. LOG OUT and back in for the change to take effect.' } catch [Microsoft.PowerShell.Commands.MemberExistsException] { Write-Host '     %USERNAME% is already in WinBiometricGroup.' } catch { Write-Host ('     WARNING: ' + $_.Exception.Message); exit 1 }"
if !ERRORLEVEL! NEQ 0 (
    echo.
    echo      Could not add %USERNAME% to WinBiometricGroup automatically.
    echo      Fallback: run the AttendAI backend as Administrator.
)

:setup_done
echo.
echo Setup complete!
echo   - If you were just added to WinBiometricGroup: LOG OUT and back in, then
echo     restart the AttendAI backend.
echo   - If you were already in the group (or as a fallback): restart the backend
echo     as Administrator.
pause
