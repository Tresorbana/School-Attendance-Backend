@echo off
setlocal
set CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" (
    echo ERROR: csc.exe not found at %CSC%
    exit /b 1
)
"%CSC%" /nologo /target:exe /optimize+ /out:FingerprintBridge.exe ^
    /r:System.dll ^
    /r:System.Core.dll ^
    /r:System.Drawing.dll ^
    /r:DPFPDevNET.dll ^
    /r:DPFPShrNET.dll ^
    FingerprintBridge.cs
if %ERRORLEVEL% EQU 0 (
    echo Build OK: FingerprintBridge.exe
) else (
    echo Build FAILED
    exit /b 1
)
endlocal
