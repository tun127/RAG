$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'Initialize first: .\scripts\setup\initialize.ps1' }
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependencies invalid. Run scripts/setup/initialize.ps1' }
& $pythonExe -c "import fastapi,uvicorn,langgraph,pydantic,jinja2,multipart,rank_bm25,pypdf,defusedxml,httpx"
if ($LASTEXITCODE -ne 0) { throw 'Missing dependencies. Run scripts/setup/initialize.ps1' }
& $pythonExe (Join-Path $projectRoot 'scripts\demo\serve.py')
exit $LASTEXITCODE
