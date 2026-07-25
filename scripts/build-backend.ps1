$ErrorActionPreference = "Stop"

$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDir = Join-Path $workspaceRoot "work\backend-venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$pyInstaller = Join-Path $venvDir "Scripts\pyinstaller.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
  $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
  if ($pythonCommand) {
    & $pythonCommand.Source -m venv $venvDir
  }
  else {
    & "py.exe" -3 -m venv $venvDir
  }
}

& $venvPython -m pip install `
  "pyinstaller==6.14.2" `
  "pymupdf==1.27.2.3" `
  "pypdf==6.13.2" `
  "pillow==11.3.0" `
  "pywin32==311"
if ($LASTEXITCODE -ne 0) {
  throw "Не удалось установить зависимости backend."
}

& $pyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name "printlantern-backend" `
  --distpath (Join-Path $workspaceRoot "backend\dist") `
  --workpath (Join-Path $workspaceRoot "work\pyinstaller") `
  --specpath (Join-Path $workspaceRoot "work\pyinstaller") `
  --hidden-import "fitz" `
  --hidden-import "pypdf" `
  --hidden-import "PIL.Image" `
  --hidden-import "PIL.ImageWin" `
  --hidden-import "win32con" `
  --hidden-import "win32print" `
  --hidden-import "win32ui" `
  (Join-Path $workspaceRoot "backend\server.py")
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller не смог собрать backend."
}
