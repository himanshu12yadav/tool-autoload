# Build the Windows exe, self-test it, and put it in release\windows. From the project root:
#     powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".\.venv\Scripts\python.exe"

& $py -m pip install --quiet pyinstaller
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name AutoReload `
    --collect-data customtkinter --collect-all playwright `
    --distpath dist --workpath build --specpath build main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# A build that cannot start its engine is not worth releasing.
$result = Join-Path $env:TEMP "autoreload-selftest.txt"
Remove-Item $result -ErrorAction SilentlyContinue
$p = Start-Process .\dist\AutoReload.exe -ArgumentList "--selftest", "`"$result`"" -PassThru
if (-not $p.WaitForExit(150000)) { $p.Kill(); throw "Self-test timed out" }
Get-Content $result
if ($p.ExitCode -ne 0) { throw "Self-test FAILED - not releasing" }

$arch = switch ($env:PROCESSOR_ARCHITECTURE) { "ARM64" { "arm64" } default { "x64" } }
New-Item -ItemType Directory -Force release\windows | Out-Null
$target = "release\windows\AutoReload-windows-$arch.exe"
Move-Item -Force dist\AutoReload.exe $target

# A zip to hand to someone (mail and chat often block a bare .exe): the exe plus a short how-to.
$stage = "release\windows\_stage\AutoReload"
Remove-Item -Recurse -Force release\windows\_stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $target "$stage\AutoReload.exe"
Copy-Item packaging\windows-readme.txt "$stage\README.txt"
$zip = "release\windows\AutoReload-windows-$arch.zip"
Compress-Archive -Path $stage -DestinationPath $zip -Force
Remove-Item -Recurse -Force release\windows\_stage
"Done: $target"
"Done: $zip"
