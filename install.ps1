<#
AirGrabber installer for Windows 10/11. In PowerShell:

    irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex

Installs Python 3.13 when no Python 3.13+ is found, downloads AirGrabber into
%LOCALAPPDATA%\Programs\AirGrabber, installs its packages, adds Start menu and desktop shortcuts
and starts it. Running it again reinstalls the latest version. Your library and settings live in
%LOCALAPPDATA%\AirGrabber and are never touched.

Written for Windows PowerShell 5.1 (what a new PC has): no &&, ternaries or ?? operators.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AirGrabber'),
    [string]$Archive,      # install from this .zip instead of downloading it (testing)
    [switch]$NoShortcuts,
    [switch]$NoLaunch
)

$Repo = 'drunkgummyboy/AirGrabber'
$Branch = 'main'
$ArchiveUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
$AppEntries = @('main.py', 'requirements.txt', 'app')  # keep in step with app/services/updates.py
$IconPath = 'app\assets\airgrabber.ico'                # keep in step with app/ui/branding.py
$PythonFallback = '3.13.15'                            # python.org build used when winget can't

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Python([string]$Exe) {
    # The real interpreter path when $Exe is Python 3.13+ with tkinter, else $null.
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $null }
    # In Windows PowerShell 5.1, stderr from a redirected native command is a terminating error
    # under 'Stop'; a Python without tkinter must just be skipped.
    $ErrorActionPreference = 'Continue'
    $probe = 'import sys, tkinter; print(sys.executable) if sys.version_info >= (3, 13) else sys.exit(1)'
    $real = & $Exe -c $probe 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $real) { return $null }
    return ($real | Select-Object -Last 1).Trim()
}

function Find-Python {
    $ErrorActionPreference = 'Continue'  # see Test-Python
    $candidates = New-Object System.Collections.Generic.List[string]
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        # "py -0p" lists every registered install: " -V:3.13 *   C:\...\python.exe"
        foreach ($line in (& $launcher.Source -0p 2>$null)) {
            if ($line -match '([A-Za-z]:\\.+?python\.exe)\s*$') { $candidates.Add($Matches[1]) }
        }
    }
    foreach ($root in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)) {
        if ($root -and (Test-Path -LiteralPath $root)) {
            Get-ChildItem -LiteralPath $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    foreach ($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        $candidates.Add($command.Source)
    }
    foreach ($candidate in $candidates) {
        $python = Test-Python $candidate
        if ($python) { return $python }
    }
    return $null
}

function Install-Python {
    Write-Step 'Installing Python 3.13 (one time)'
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --id Python.Python.3.13 --exact --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        $python = Find-Python
        if ($python) { return $python }
        Write-Host 'winget could not install Python; trying python.org instead.' -ForegroundColor Yellow
    }
    $url = "https://www.python.org/ftp/python/$PythonFallback/python-$PythonFallback-amd64.exe"
    $setup = Join-Path ([IO.Path]::GetTempPath()) "python-$PythonFallback-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
    try {
        $process = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0'
        )
        if ($process.ExitCode -ne 0) { throw "The Python installer failed (exit code $($process.ExitCode))." }
    } finally {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
    }
    $python = Find-Python
    if (-not $python) { throw 'Python was installed, but could not be found. Open a new PowerShell window and run the command again.' }
    return $python
}

function Install-App {
    $work = Join-Path ([IO.Path]::GetTempPath()) ('airgrabber-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        if ($Archive) {
            $zip = (Resolve-Path -LiteralPath $Archive).Path
        } else {
            Write-Step 'Downloading AirGrabber'
            $zip = Join-Path $work 'airgrabber.zip'
            Invoke-WebRequest -Uri $ArchiveUrl -OutFile $zip -UseBasicParsing
        }
        $unpacked = Join-Path $work 'unpacked'
        Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
        $source = @(Get-ChildItem -LiteralPath $unpacked -Directory)
        if ($source.Count -ne 1) { throw 'The download has an unexpected layout.' }
        foreach ($entry in $AppEntries) {
            if (-not (Test-Path -LiteralPath (Join-Path $source[0].FullName $entry))) {
                throw "The download is incomplete: $entry is missing."
            }
        }
        Write-Step "Installing into $InstallDir"
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        foreach ($entry in $AppEntries) {
            $target = Join-Path $InstallDir $entry
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            Copy-Item -LiteralPath (Join-Path $source[0].FullName $entry) -Destination $target -Recurse
        }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function New-Shortcut([string]$Path, [string]$PythonW) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $PythonW
    $link.Arguments = '"' + (Join-Path $InstallDir 'main.py') + '"'
    $link.WorkingDirectory = $InstallDir
    $link.IconLocation = (Join-Path $InstallDir $IconPath) + ',0'
    $link.Description = 'Track your shows. Grab your movies.'
    $link.Save()
}

function Install-AirGrabber {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest crawls with a progress bar in 5.1
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    Write-Step 'Looking for Python 3.13 or newer'
    $python = Find-Python
    if ($python) { Write-Host "    found $python" } else { $python = Install-Python }

    Install-App

    Write-Step 'Installing the Python packages AirGrabber needs'
    & $python -m pip install --disable-pip-version-check --no-input --quiet -r (Join-Path $InstallDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installing the Python packages failed (see the messages above).' }

    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }
    if (-not $NoShortcuts) {
        Write-Step 'Adding Start menu and desktop shortcuts'
        New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'AirGrabber.lnk') $pythonw
        New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'AirGrabber.lnk') $pythonw
    }

    Write-Host ''
    Write-Host 'AirGrabber is installed.' -ForegroundColor Green
    Write-Host 'Updates: Settings > Check for updates (or run this command again).'
    if (-not $NoLaunch) {
        Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $InstallDir 'main.py') + '"') -WorkingDirectory $InstallDir
    }
}

# No `exit` anywhere: under `irm | iex` it would close the user's PowerShell window.
try {
    Install-AirGrabber
} catch {
    Write-Host ''
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
}
