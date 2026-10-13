<#
AirGrabber installer for Windows 10/11. Press Win+R and run:

    powershell -c "irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex"

(or, already in PowerShell: irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex)

Installs Python 3.13 when no Python 3.13+ is found, downloads AirGrabber into
%LOCALAPPDATA%\Programs\AirGrabber, installs its packages, adds Start menu and desktop shortcuts,
registers it under Windows' "Installed apps" (current user; uninstaller: app\uninstall.ps1) and
starts it. Running it again reinstalls the latest version. Your library and settings live in
%LOCALAPPDATA%\AirGrabber and are never touched.

It also offers to install Prowlarr (more torrent sites; needs administrator permission) unless it is
already there: -Prowlarr installs it without asking, -NoProwlarr skips it (and wins over -Prowlarr).

Written for Windows PowerShell 5.1 (what a new PC has): no &&, ternaries or ?? operators.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AirGrabber'),
    [string]$Archive,      # install from this .zip instead of downloading it (testing)
    [switch]$NoShortcuts,
    [switch]$NoLaunch,
    [switch]$Prowlarr,
    [switch]$NoProwlarr
)

$Repo = 'drunkgummyboy/AirGrabber'
$Branch = 'main'
$ArchiveUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
$AppEntries = @('main.py', 'requirements.txt', 'app')  # keep in step with app/services/updates.py
$IconPath = 'app\assets\airgrabber.ico'                # keep in step with app/ui/branding.py
$PythonFallback = '3.13.15'                            # python.org build used when winget can't
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AirGrabber'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Python([string]$Exe, [switch]$Tested) {
    # The real interpreter path when $Exe is Python 3.13+ with tkinter (with -Tested: exactly
    # 3.13, the version AirGrabber is tested on), else $null.
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $null }
    # In Windows PowerShell 5.1, stderr from a redirected native command is a terminating error
    # under 'Stop'; a Python without tkinter must just be skipped.
    $ErrorActionPreference = 'Continue'
    $wanted = 'sys.version_info[:2] == (3, 13)'
    if (-not $Tested) { $wanted = 'sys.version_info >= (3, 13)' }
    $probe = "import sys, tkinter; print(sys.executable) if $wanted else sys.exit(1)"
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
    # Prefer the tested Python 3.13; a newer one is only used when 3.13 isn't there.
    foreach ($candidate in $candidates) {
        $python = Test-Python $candidate -Tested
        if ($python) { return $python }
    }
    foreach ($candidate in $candidates) {
        $python = Test-Python $candidate
        if ($python) { return $python }
    }
    return $null
}

function Install-Python {
    Write-Step 'Installing Python, which AirGrabber runs on (only needed once; this can take a few minutes)'
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        # Out-Host: winget's output must not become part of this function's return value.
        & $winget.Source install --id Python.Python.3.13 --exact --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Host
        $python = Find-Python
        if ($python) { return $python }
        Write-Host 'That did not work; trying another way to install Python.' -ForegroundColor Yellow
    }
    $url = "https://www.python.org/ftp/python/$PythonFallback/python-$PythonFallback-amd64.exe"
    $setup = Join-Path ([IO.Path]::GetTempPath()) "python-$PythonFallback-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
    try {
        $process = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0'
        )
        if ($process.ExitCode -ne 0) { throw "Python could not be installed (error code $($process.ExitCode))." }
    } finally {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
    }
    $python = Find-Python
    if (-not $python) { throw 'Python was installed, but cannot be found yet. Please run the install command again.' }
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
        if ($source.Count -ne 1) { throw 'The download looks damaged. Please try again.' }
        foreach ($entry in $AppEntries) {
            if (-not (Test-Path -LiteralPath (Join-Path $source[0].FullName $entry))) {
                throw "The download is incomplete ($entry is missing). Please try again."
            }
        }
        Write-Step 'Copying AirGrabber to your computer'
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

function Register-Uninstaller {
    # The "Installed apps" entry (current user, so no administrator permission needed).
    # The app may later refresh DisplayVersion itself after a self-update, so keep the key path
    # and value names stable.
    $root = (Resolve-Path -LiteralPath $InstallDir).Path.TrimEnd('\')
    $version = ''
    $init = Join-Path $root 'app\__init__.py'
    if (Test-Path -LiteralPath $init) {
        $match = Select-String -LiteralPath $init -Pattern '^__version__\s*=\s*[''"]([^''"]+)[''"]' |
            Select-Object -First 1
        if ($match) { $version = $match.Matches[0].Groups[1].Value }
    }
    $bytes = (Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if (-not $bytes) { $bytes = 0 }
    $sizeKb = [int][Math]::Ceiling($bytes / 1KB)
    $uninstall = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
        (Join-Path $root 'app\uninstall.ps1') + '"'

    if (-not (Test-Path -LiteralPath $UninstallKey)) { New-Item -Path $UninstallKey -Force | Out-Null }
    $strings = [ordered]@{
        DisplayName          = 'AirGrabber'
        DisplayVersion       = $version
        Publisher            = 'drunkgummyboy'
        DisplayIcon          = (Join-Path $root $IconPath)
        InstallLocation      = $root
        URLInfoAbout         = "https://github.com/$Repo"
        UninstallString      = $uninstall
        QuietUninstallString = $uninstall + ' -Quiet'
    }
    foreach ($name in $strings.Keys) {
        New-ItemProperty -LiteralPath $UninstallKey -Name $name -Value $strings[$name] -PropertyType String -Force | Out-Null
    }
    $dwords = [ordered]@{ NoModify = 1; NoRepair = 1; EstimatedSize = $sizeKb }
    foreach ($name in $dwords.Keys) {
        New-ItemProperty -LiteralPath $UninstallKey -Name $name -Value $dwords[$name] -PropertyType DWord -Force | Out-Null
    }
}

function Install-ProwlarrIfWanted {
    # Optional extra: a Prowlarr failure must never fail the AirGrabber install.
    $ErrorActionPreference = 'Continue'  # see Test-Python
    $installed = (Test-Path -LiteralPath 'C:\ProgramData\Prowlarr\config.xml') -or
        [bool](Get-Service -Name Prowlarr -ErrorAction SilentlyContinue)
    if ($installed) {
        Write-Host '    The search helper (Prowlarr) is already installed; AirGrabber uses it automatically.'
        return
    }
    if ($NoProwlarr) { return }
    if (-not $Prowlarr) {
        $answer = ''
        try {
            $answer = Read-Host 'Search more websites for downloads? This adds a free helper program (Prowlarr). Windows will ask for permission. [y/N]'
        } catch {
            $answer = ''  # non-interactive host
        }
        if (-not $answer) { $answer = '' }
        if (@('y', 'yes') -notcontains $answer.Trim().ToLowerInvariant()) { return }
    }

    Write-Step 'Installing the search helper (Prowlarr)'
    $ok = $false
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            # Machine scope: Windows shows a UAC prompt for the Prowlarr service.
            & $winget.Source install --id TeamProwlarr.Prowlarr --exact --silent `
                --accept-package-agreements --accept-source-agreements | Out-Host
            $ok = ($LASTEXITCODE -eq 0)
        } catch {
            $ok = $false
        }
    }
    if (-not $ok) {
        Write-Host 'The search helper could not be installed automatically. You can get it later from https://prowlarr.com/#downloads-v3-windows; AirGrabber finds it by itself.' -ForegroundColor Yellow
        return
    }

    $deadline = (Get-Date).AddSeconds(90)
    $running = $false
    while (-not $running -and (Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri 'http://localhost:9696/ping' -UseBasicParsing -TimeoutSec 3
            $running = $true
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if ($running) {
        Write-Host '    The search helper is running. AirGrabber sets it up the next time it starts.'
    } else {
        Write-Host '    The search helper was installed. AirGrabber uses it once it is running.'
    }
}

function Install-AirGrabber {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest crawls with a progress bar in 5.1
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    Write-Step 'Checking for Python'
    $python = Find-Python
    if ($python) { Write-Host '    Python is already installed.' } else { $python = Install-Python }

    Install-App

    Write-Step 'Installing the parts AirGrabber needs (this can take a minute)'
    # --no-warn-script-location: pip's "not on PATH" warnings are harmless but look alarming.
    & $python -m pip install --disable-pip-version-check --no-input --quiet --no-warn-script-location -r (Join-Path $InstallDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Some parts AirGrabber needs could not be installed (see the messages above). Check your internet connection and try again.' }

    Install-ProwlarrIfWanted

    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }
    if (-not $NoShortcuts) {
        Write-Step 'Adding AirGrabber to the Start menu and your desktop'
        New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'AirGrabber.lnk') $pythonw
        New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'AirGrabber.lnk') $pythonw
    }

    Write-Step 'Adding AirGrabber to Installed apps in Windows Settings'
    try {
        Register-Uninstaller
    } catch {
        # AirGrabber itself works; only the Settings entry is missing.
        Write-Host '    AirGrabber could not be added to Installed apps, but it is installed and works.' -ForegroundColor Yellow
    }

    Write-Host ''
    if ($NoShortcuts) {
        Write-Host 'AirGrabber is ready.' -ForegroundColor Green
    } else {
        Write-Host "AirGrabber is ready. You'll find it in the Start menu and on your desktop." -ForegroundColor Green
    }
    Write-Host 'It updates itself.'
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
    # Keep a Win+R window open so the message can be read; a non-interactive host throws instead.
    try { $null = Read-Host 'Press Enter to close' } catch { }
}
