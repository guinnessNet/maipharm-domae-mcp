<#
도매 로컬 exe 빌드 (B-4 Task 11, 결정 F1·F7). 커밋된 트리만 빌드한다.

사용:
  powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Kind test|release -Python <python.exe> -Out <dir> [-Builder nuitka|pyinstaller] [-LogDir <dir>]

순서: Python 3.12.10 확인 → git 트리 깨끗한지 확인 → 프론트 빌드(결과가 커밋된 static 과 같아야 함) →
_build_info 생성 → Nuitka onefile(또는 PyInstaller, UPX 끔) → _build_info 삭제 → 빌드 보고서 검사 → sha256.
빌드 프로세스에서만 CC 환경변수를 지운다(B0 §5). 산출물·로그는 레포 밖 -Out/-LogDir 에 둔다.
네이티브 명령은 cmd /c 로 돌려 stderr 출력이 PowerShell 5.1 오류로 바뀌지 않게 하고 종료 코드로만 판정한다.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("test", "release")][string]$Kind,
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Out,
    [ValidateSet("nuitka", "pyinstaller")][string]$Builder = "nuitka",
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Out = [IO.Path]::GetFullPath($Out)
if (-not $LogDir) { $LogDir = $Out }
$LogDir = [IO.Path]::GetFullPath($LogDir)
$Python = [IO.Path]::GetFullPath($Python)
New-Item -ItemType Directory -Force $Out | Out-Null
New-Item -ItemType Directory -Force $LogDir | Out-Null
$BuildInfo = Join-Path $Repo "src\domae_mcp\_build_info.py"
$script:Failed = $null

function Fail([string]$msg) { throw "BUILD FAIL: $msg" }

function Run([string]$cmdline, [string]$log) {
    # stderr 는 로그로만 보낸다. 판정은 종료 코드로 한다.
    cmd /c "$cmdline > `"$log`" 2>&1"
    return $LASTEXITCODE
}

Push-Location $Repo
try {
    # 1. Python
    $ver = (& $Python -c "import platform; print(platform.python_version())").Trim()
    if ($LASTEXITCODE -ne 0 -or $ver -ne "3.12.10") { Fail "python version '$ver' (3.12.10 필요)" }

    # 2. 커밋된 트리만
    $dirty = git status --porcelain
    if ($LASTEXITCODE -ne 0) { Fail "git status" }
    if ($dirty) { Fail "작업 트리가 깨끗하지 않다(커밋된 트리만 빌드한다)" }
    $sha = (git rev-parse --short=12 HEAD)
    if ($LASTEXITCODE -ne 0) { Fail "git rev-parse" }
    $sha = $sha.Trim()

    # 3. 프론트 빌드 → static 이 커밋과 같아야 한다
    $fe = Join-Path $Repo "frontend"
    if (-not (Test-Path (Join-Path $fe "node_modules"))) {
        if ((Run "cd /d `"$fe`" && npm ci" (Join-Path $LogDir "npm-ci.log")) -ne 0) { Fail "npm ci" }
    }
    if ((Run "cd /d `"$fe`" && npm run build" (Join-Path $LogDir "frontend-build.log")) -ne 0) { Fail "npm run build" }
    $static = Join-Path $Repo "src\domae_mcp\static"
    Get-ChildItem $static | Remove-Item -Recurse -Force
    Copy-Item -Recurse -Force (Join-Path $fe "dist\*") $static
    $dirty = git status --porcelain
    if ($LASTEXITCODE -ne 0) { Fail "git status" }
    if ($dirty) { Fail "프론트 빌드 결과가 커밋된 static 과 다르다(되돌리기: git checkout -- src/domae_mcp/static 후 다시 커밋)" }

    # 4. 빌드 종류
    & $Python scripts\gen_build_info.py --kind $Kind
    if ($LASTEXITCODE -ne 0) { Fail "gen_build_info" }

    # 5. 빌드
    $env:PYTHONPATH = Join-Path $Repo "src"
    if (Test-Path Env:CC) { Remove-Item Env:CC }
    $log = Join-Path $LogDir "build-$Builder-$Kind.log"
    $exe = Join-Path $Out "MaipharmDomae.exe"
    $start = Get-Date
    if ($Builder -eq "nuitka") {
        $report = Join-Path $Out "nuitka-report.xml"
        $yes = if ($env:CI) { "--assume-yes-for-downloads" } else { "" }
        $nargs = @(
            "-m nuitka --onefile --msvc=latest --windows-console-mode=disable $yes",
            "--include-package=domae_mcp --include-package-data=domae_mcp",
            "--include-module=domae_mcp.v2.crawler_deps --include-module=domae_mcp._build_info",
            "--include-package=uvicorn --include-module=pystray._win32 --enable-plugin=tk-inter",
            "--nofollow-import-to=domae_mcp.cloud --nofollow-import-to=domae_mcp.core.crawlers.loader",
            "--nofollow-import-to=domae_mcp.core.crawlers._client_key --nofollow-import-to=pytest",
            "--nofollow-import-to=_pytest --nofollow-import-to=fakeredis --nofollow-import-to=tests",
            "--output-filename=MaipharmDomae.exe `"--output-dir=$Out`" `"--report=$report`"",
            "--no-progressbar src\domae_mcp\desktop\app.py"
        ) -join " "
        $rc = Run "`"$Python`" $nargs" $log
    } else {
        $work = Join-Path $Repo "build"
        $report = Join-Path $work "domae"   # PyInstaller 작업 폴더는 spec 파일 이름을 따른다
        $rc = Run "`"$Python`" -m PyInstaller --noconfirm --distpath `"$Out`" --workpath `"$work`" domae.spec" $log
    }
    $secs = [int]((Get-Date) - $start).TotalSeconds
    if ($rc -ne 0) { Fail "$Builder rc=$rc (log: $log)" }
    if (-not (Test-Path $exe)) { Fail "exe 없음" }

    # 6. 빌드 보고서 검사
    if ($Builder -eq "pyinstaller") {
        $toc = Get-ChildItem -Path $report -Filter "PYZ-*.toc" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $toc) { Fail "PyInstaller TOC 없음" }
        $report = $toc.FullName
    }
    & $Python scripts\check_build_report.py $report --kind $Builder
    if ($LASTEXITCODE -ne 0) { Fail "빌드 보고서 검사" }

    # 7. 결과
    $hash = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLower()
    $size = (Get-Item $exe).Length
    "$hash  MaipharmDomae.exe" | Out-File -Encoding ascii (Join-Path $Out "MaipharmDomae.exe.sha256")
    $summary = [ordered]@{ kind = $Kind; builder = $Builder; git_sha = $sha; seconds = $secs; size = $size; sha256 = $hash }
    $summary | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $Out "build-summary.json")
    Write-Host "BUILD OK kind=$Kind builder=$Builder sha=$sha seconds=$secs size=$size sha256=$hash"
} catch {
    $script:Failed = "$_"
} finally {
    if (Test-Path $BuildInfo) { Remove-Item -Force $BuildInfo }
    Pop-Location
}
if ($script:Failed) {
    Write-Host $script:Failed
    exit 1
}
exit 0
