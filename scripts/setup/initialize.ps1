param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) {
    & $Python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ is required.' }
}
$constraints = Join-Path $projectRoot 'docs\organization\environment-lock.txt'
& $pythonExe -X utf8 -m pip install -c $constraints -e "$projectRoot[dev,contract-e2e]"
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; cached wheels or network access required.' }
& $pythonExe -m pip check
exit $LASTEXITCODE
