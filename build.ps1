$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

# Keep PyInstaller's work files out of the repo so only dist\Tokenmon.exe remains.
$work = Join-Path $env:TEMP "tokenmon-build"
New-Item -ItemType Directory -Force $work | Out-Null

# One version number, defined in src\tokenmon\__init__.py, also stamped into the exe's file properties.
$version = (python -c "import sys; sys.path.insert(0, r'$repoRoot\src'); import tokenmon; print(tokenmon.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw "Could not read the app version (got '$version')" }
$parts = (($version.Split('.') + @('0')) -join ', ')
$versionFile = Join-Path $work "version_info.txt"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Internal'),
      StringStruct('FileDescription', 'Tokenmon - Claude and Codex usage monitor'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'Tokenmon'),
      StringStruct('OriginalFilename', 'Tokenmon.exe'),
      StringStruct('ProductName', 'Tokenmon'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
# PyInstaller evals this file, so write it without a byte-order mark.
[IO.File]::WriteAllText($versionFile, $versionInfo, (New-Object System.Text.UTF8Encoding $false))

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name Tokenmon `
    --hidden-import pystray._win32 `
    --hidden-import PIL._tkinter_finder `
    --version-file $versionFile `
    --paths "$repoRoot\src" `
    --workpath $work `
    --specpath $work `
    "$repoRoot\src\tokenmon\gui_main.py"
if ($LASTEXITCODE -ne 0) { throw "Build failed with exit code $LASTEXITCODE" }

Remove-Item "$repoRoot\dist\tokenmon-cli.exe" -ErrorAction SilentlyContinue
Write-Host "Built: $repoRoot\dist\Tokenmon.exe  (v$version)"
