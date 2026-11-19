param([int]$Port = 8765, [switch]$NoOpen)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($env:PAPERLOOM_PYTHON) {
    $pythonPath = $env:PAPERLOOM_PYTHON
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    $pythonPath = if ($pythonCmd) { $pythonCmd.Source } else { $null }
    if (-not $pythonPath -or $pythonPath -like '*WindowsApps*') {
        $pyCmd = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyCmd -and $pyCmd.Source -notlike '*WindowsApps*') {
            $resolved = & $pyCmd.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonPath = $resolved.Trim() }
        }
    }
    if (-not $pythonPath -or $pythonPath -like '*WindowsApps*') {
        $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundled) { $pythonPath = $bundled }
    }
}
if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) { throw 'Install Python 3.11+ or set PAPERLOOM_PYTHON to python.exe.' }
& $pythonPath -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ required.' }
if (-not (Test-Path -LiteralPath '.runtime\fastapi') -or -not (Test-Path -LiteralPath '.runtime\pymupdf')) {
    & $pythonPath -m pip install --target .runtime -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency install failed. Check network access to PyPI and run again.' }
}
$launchArgs = @('run.py', 'serve', '--port', "$Port")
if (-not $NoOpen) { $launchArgs += '--open' }
& $pythonPath @launchArgs
