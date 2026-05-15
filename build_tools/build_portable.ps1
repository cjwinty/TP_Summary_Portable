$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Spec = Join-Path $RepoRoot "TP Query.spec"
$SeedExe = Join-Path $RepoRoot "build_tools\portable-test\TP Query\TP Query.exe"
$SeedInternal = Join-Path $RepoRoot "build_tools\portable-test\TP Query\_internal"
$TkinterPyc = Join-Path $RepoRoot "build_tools\tkinter_pyc"
$DistDir = Join-Path $RepoRoot "dist\TP Query"
$ZipPath = Join-Path $RepoRoot "dist\TP Query Portable.zip"

function Require-Path {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required build asset is missing: $Path"
    }
}

function Remove-DistFile {
    param([string] $Name)
    $target = Join-Path $DistDir $Name
    if (-not (Test-Path -LiteralPath $target)) {
        return
    }

    $resolvedDist = (Resolve-Path -LiteralPath $DistDir).Path
    $resolvedTarget = (Resolve-Path -LiteralPath $target).Path
    if (-not $resolvedTarget.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a file outside dist: $resolvedTarget"
    }

    Remove-Item -LiteralPath $resolvedTarget -Force
}

Push-Location $RepoRoot
try {
    Require-Path $Python
    Require-Path $Spec
    Require-Path $SeedExe
    Require-Path (Join-Path $SeedInternal "_tkinter.pyd")
    Require-Path (Join-Path $SeedInternal "tcl86t.dll")
    Require-Path (Join-Path $SeedInternal "tk86t.dll")
    Require-Path (Join-Path $SeedInternal "_tcl_data\init.tcl")
    Require-Path (Join-Path $SeedInternal "_tk_data\tk.tcl")

    $prepareTkinter = @"
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
from pathlib import Path
import importlib.util
import marshal
import shutil
import struct
import time

root = Path(r"$TkinterPyc")
if root.exists():
    shutil.rmtree(root)
root.mkdir(parents=True)

archive = CArchiveReader(r"$SeedExe")
pyz_name = next((name for name in archive.toc if name == "PYZ.pyz" or name.endswith("PYZ.pyz")), None)
if pyz_name is None:
    raise RuntimeError("Could not find PYZ.pyz in the Tk seed executable")

pyz_path = root.parent / "_tkinter_seed.pyz"
pyz_path.write_bytes(archive.extract(pyz_name))
try:
    pyz = ZlibArchiveReader(str(pyz_path))
    header = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(time.time()), 0)
    written = 0
    for module_name in sorted(pyz.toc):
        if module_name != "tkinter" and not module_name.startswith("tkinter."):
            continue
        code = pyz.extract(module_name)
        parts = module_name.split(".")[1:]
        output = root / "__init__.pyc" if module_name == "tkinter" else root.joinpath(*parts).with_suffix(".pyc")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(header + marshal.dumps(code))
        written += 1
    if written == 0:
        raise RuntimeError("No tkinter bytecode was found in the Tk seed executable")
finally:
    pyz_path.unlink(missing_ok=True)
"@
    $prepareTkinter | & $Python -

    & $Python -m PyInstaller --noconfirm --clean $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    Copy-Item -LiteralPath (Join-Path $RepoRoot ".env.example") -Destination (Join-Path $DistDir ".env.example") -Force

    $requiredOutput = @(
        "TP Query.exe",
        "_internal\_tkinter.pyd",
        "_internal\tcl86t.dll",
        "_internal\tk86t.dll",
        "_internal\_tcl_data\init.tcl",
        "_internal\_tk_data\tk.tcl",
        "_internal\tkinter\__init__.pyc",
        ".env.example"
    )
    foreach ($item in $requiredOutput) {
        Require-Path (Join-Path $DistDir $item)
    }

    $oldPath = $env:PATH
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        $process = Start-Process -FilePath (Join-Path $DistDir "TP Query.exe") -WorkingDirectory $DistDir -PassThru -WindowStyle Hidden
        Start-Sleep -Seconds 8
        if ($process.HasExited) {
            throw "Smoke test failed: app exited with code $($process.ExitCode)"
        }
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit(5000)
    }
    finally {
        $env:PATH = $oldPath
    }

    foreach ($name in @("tp_cache.db", "tp_cache.db-shm", "tp_cache.db-wal", "tp_query_error.log", "user_config.json")) {
        Remove-DistFile $name
    }

    Compress-Archive -Path $DistDir -DestinationPath $ZipPath -Force
    Write-Host "Portable build complete:"
    Write-Host "  $DistDir"
    Write-Host "  $ZipPath"
}
finally {
    Pop-Location
}
