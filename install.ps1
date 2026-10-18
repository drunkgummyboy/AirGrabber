<#
AirGrabber installer for Windows 10/11. Open Windows PowerShell (Start, type powershell) and run:

    irm https://raw.githubusercontent.com/drunkgummyboy/AirGrabber/main/install.ps1 | iex

(Not via the Run dialog: security software blocks download commands started from Win+R, because
scam pages ask people to paste them there.)

Sets up winget (Windows' app installer) when it is missing, installs Python 3.13 when no
Python 3.13+ is found, downloads AirGrabber into %LOCALAPPDATA%\Programs\AirGrabber, installs its
packages, adds Start menu and desktop shortcuts, registers it under Windows' "Installed apps"
(current user; uninstaller: app\uninstall.ps1) and starts it. Running it again reinstalls the latest
version. Your library and settings live in %LOCALAPPDATA%\AirGrabber and are never touched.

It also offers to install Prowlarr (more torrent sites; needs administrator permission) in a small
window unless it is already there: -Prowlarr installs it without asking, -NoProwlarr skips it (and
wins over -Prowlarr). When the install fails, a window says so as well as the console.

Written for Windows PowerShell 5.1 (what a new PC has): no &&, ternaries or ?? operators. ASCII only,
so a copy run with -File (no BOM) reads the same.
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
$AppId = 'drunkgummyboy.AirGrabber'                    # keep in step with app/identity.py (APP_USER_MODEL_ID)
$AppIdHelper = 'app\shortcut_app_id.cs'                # also app/services/powershell.py (SHORTCUT_HELPER)
$PythonFallback = '3.13.15'                            # python.org build used when winget can't
$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AirGrabber'
# winget for PCs without it: App Installer (which brings winget.exe) from winget's own release, plus
# the frameworks that release lists in DesktopAppInstaller_Dependencies.json and ships in
# DesktopAppInstaller_Dependencies.zip (v1.29: VCLibs 14.00, VCLibs UWPDesktop, Windows App Runtime
# 1.8; no UI.Xaml any more). Pinned, so the two downloads always belong together.
$WingetRelease = 'https://github.com/microsoft/winget-cli/releases/download/v1.29.380'
$AppInstallerFamily = 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Add-WindowsAppsToPath {
    # winget.exe is an app alias in %LOCALAPPDATA%\Microsoft\WindowsApps, which a fresh session's
    # PATH may not have yet.
    $apps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    $path = [string]$env:PATH
    foreach ($entry in $path.Split(';')) {
        if ($entry.Trim().TrimEnd('\') -eq $apps) { return }
    }
    if ($path) { $env:PATH = $path.TrimEnd(';') + ';' + $apps } else { $env:PATH = $apps }
}

function Test-Winget {
    return [bool](Get-Command winget.exe -ErrorAction SilentlyContinue)
}

function Wait-Winget([int]$Seconds) {
    # $true once winget.exe can be found; Windows may add the alias a moment after an install.
    for ($second = 0; $second -lt $Seconds; $second++) {
        if (Test-Winget) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-Winget)
}

function Get-WingetArchitecture {
    # winget's frameworks come per processor: arm64 on an ARM PC, else x64.
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { return 'arm64' }
    return 'x64'
}

function Install-WingetFromGitHub {
    # Downloads App Installer and its frameworks from winget's release ($WingetRelease) and adds the
    # frameworks for this PC's processor, then App Installer. Throws when App Installer could not be
    # added. The downloads are removed either way.
    $arch = Get-WingetArchitecture
    $work = Join-Path ([IO.Path]::GetTempPath()) ('airgrabber-winget-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        Write-Host '    Downloading it from Microsoft (about 300 MB, so this can take a few minutes).'
        $frameworksZip = Join-Path $work 'DesktopAppInstaller_Dependencies.zip'
        $bundle = Join-Path $work "$AppInstallerFamily.msixbundle"
        Invoke-WebRequest -Uri "$WingetRelease/DesktopAppInstaller_Dependencies.zip" -OutFile $frameworksZip -UseBasicParsing
        Invoke-WebRequest -Uri "$WingetRelease/$AppInstallerFamily.msixbundle" -OutFile $bundle -UseBasicParsing
        $unpacked = Join-Path $work 'dependencies'
        Expand-Archive -LiteralPath $frameworksZip -DestinationPath $unpacked -Force
        # By name: both VCLibs come before the Windows App Runtime.
        $frameworks = @(Get-ChildItem -LiteralPath (Join-Path $unpacked $arch) -Filter '*.appx' | Sort-Object Name)
        if ($frameworks.Count -eq 0) { throw "winget's download has no frameworks for $arch." }
        foreach ($framework in $frameworks) {
            try {
                $null = Add-AppxPackage -Path $framework.FullName
            } catch {
                # Mostly "a higher version is already installed", which App Installer can use too.
                Write-Verbose ('Framework ' + $framework.Name + ' not added: ' + $_.Exception.Message)
            }
        }
        $null = Add-AppxPackage -Path $bundle
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Initialize-Winget {
    # Makes sure winget is there (it installs Python and Prowlarr): registers the App Installer that
    # comes with Windows but may not be set up yet (common on a new Windows 11), else downloads it
    # from Microsoft. Never fails the install: without winget, Python comes from python.org and
    # Prowlarr shows where to get it. Returns 'found', 'registered', 'downloaded' or 'failed'.
    $ErrorActionPreference = 'Stop'  # every failure below lands in a catch
    try {
        Add-WindowsAppsToPath
    } catch {
        Write-Verbose ('WindowsApps not added to PATH: ' + $_.Exception.Message)
    }
    if (Test-Winget) { return 'found' }

    Write-Step "Getting Windows' app installer (winget)..."
    $registered = $false
    try {
        $null = Add-AppxPackage -RegisterByFamilyName -MainPackage $AppInstallerFamily
        $registered = $true
    } catch {
        Write-Verbose ('App Installer not registered: ' + $_.Exception.Message)
    }
    $wait = 0
    if ($registered) { $wait = 5 }
    if (Wait-Winget $wait) { return 'registered' }

    try {
        $null = Install-WingetFromGitHub
        if (Wait-Winget 10) { return 'downloaded' }
    } catch {
        Write-Verbose ('winget not downloaded: ' + $_.Exception.Message)
    }
    Write-Host "Couldn't add winget; continuing without it." -ForegroundColor Yellow
    return 'failed'
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
    Set-ShortcutAppId $Path
}

function Set-ShortcutAppId([string]$Path) {
    # AirGrabber's AppUserModelID (app/identity.py) on the shortcut: Windows takes a notification's
    # name and icon from the Start-menu shortcut that has it, and taskbar pins group by it.
    # The C# helper comes with the app just installed, compiled once per run. Optional: without
    # it the shortcut still works (and AirGrabber adds the ID to its Start-menu shortcut itself).
    try {
        if (-not ('AirGrabber.ShortcutAppId' -as [type])) {
            $helper = (Resolve-Path -LiteralPath (Join-Path $InstallDir $AppIdHelper)).ProviderPath
            Add-Type -TypeDefinition ([IO.File]::ReadAllText($helper))
        }
        [AirGrabber.ShortcutAppId]::Set($Path, $AppId)
    } catch {
        Write-Verbose ('No AppUserModelID on ' + $Path + ': ' + $_.Exception.Message)
    }
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

function Initialize-SetupWindow {
    # $true when this PowerShell can show a window: a user's desktop, not started with
    # -NonInteractive, and Windows Forms loaded. Also makes the process DPI-aware (before its first
    # window) so the windows are sharp at 150 %; if that fails they still work, only blurry.
    $ErrorActionPreference = 'Stop'
    try {
        if (-not [Environment]::UserInteractive) { return $false }
        foreach ($argument in [Environment]::GetCommandLineArgs()) {
            $name = ([string]$argument).TrimStart('-', '/').ToLowerInvariant()
            if ($name.Length -ge 4 -and 'noninteractive'.StartsWith($name)) { return $false }  # -noni...
        }
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    } catch {
        return $false
    }
    try {
        if (-not ('AirGrabberSetup.NativeMethods' -as [type])) {
            Add-Type -Namespace AirGrabberSetup -Name NativeMethods -MemberDefinition (
                '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' +
                '[DllImport("dwmapi.dll")] public static extern int DwmSetWindowAttribute(' +
                'IntPtr hwnd, int attribute, ref int value, int size);')
        }
        $null = [AirGrabberSetup.NativeMethods]::SetProcessDPIAware()
    } catch {
        Write-Verbose ('Setup windows may look blurry: ' + $_.Exception.Message)
    }
    try { [Windows.Forms.Application]::EnableVisualStyles() } catch { }
    return $true
}

function Set-DarkTitleBar($Handle) {
    # Windows 10/11 dark title bar, as AirGrabber's own windows have (app/bootstrap.py). Optional.
    try {
        $on = 1
        foreach ($attribute in @(20, 19)) {  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 before Windows 10 20H1)
            if ([AirGrabberSetup.NativeMethods]::DwmSetWindowAttribute($Handle, $attribute, [ref]$on, 4) -eq 0) { return }
        }
    } catch {
        Write-Verbose ('No dark title bar: ' + $_.Exception.Message)
    }
}

function New-SetupDialog {
    # An AirGrabber setup window in the app's dark style, not shown yet: a heading, paragraphs, an
    # optional small selectable details line, and buttons at the bottom right. $Buttons, left to
    # right: hashtables with Text, Result (a DialogResult name) and optionally Primary. Enter presses
    # the $AcceptResult button, Escape the $CancelResult one. Sizes are for 100 % (96 DPI); Windows
    # Forms scales them to the screen (AutoScaleMode Dpi).
    param(
        [string]$Heading,
        [string[]]$Paragraphs,
        [string]$Details = '',
        [object[]]$Buttons,
        [string]$AcceptResult,
        [string]$CancelResult
    )
    $background = [Drawing.ColorTranslator]::FromHtml('#161616')  # app/ui/theme.py BG
    $accent = [Drawing.ColorTranslator]::FromHtml('#4E6A85')      # ACCENT / EDGE
    $accentHover = [Drawing.ColorTranslator]::FromHtml('#427839') # ACCENT_HOVER
    $surface = [Drawing.ColorTranslator]::FromHtml('#242424')     # SURFACE (outlined button hover)
    $muted = [Drawing.ColorTranslator]::FromHtml('#A4B2C6')       # TEXT_MUTED
    $faint = [Drawing.ColorTranslator]::FromHtml('#8A8A8A')

    $form = New-Object Windows.Forms.Form
    $form.SuspendLayout()
    $form.AutoScaleDimensions = New-Object Drawing.SizeF(96, 96)
    $form.AutoScaleMode = [Windows.Forms.AutoScaleMode]::Dpi
    $form.Text = 'AirGrabber setup'
    $form.BackColor = $background
    $form.ForeColor = [Drawing.Color]::White
    $form.Font = New-Object Drawing.Font('Segoe UI', 10.5)
    $form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = [Windows.Forms.FormStartPosition]::CenterScreen
    $form.TopMost = $true  # above the PowerShell window
    $form.AutoSize = $true
    $form.AutoSizeMode = [Windows.Forms.AutoSizeMode]::GrowAndShrink
    $form.Padding = New-Object Windows.Forms.Padding(30, 26, 30, 24)

    # One 420-wide column (480 minus the padding); each row as tall as its text. 10 between the
    # blocks of text, 22 above the buttons.
    $table = New-Object Windows.Forms.TableLayoutPanel
    $table.ColumnCount = 1
    [void]$table.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle([Windows.Forms.SizeType]::Absolute, 420)))
    $table.AutoSize = $true
    $table.AutoSizeMode = [Windows.Forms.AutoSizeMode]::GrowAndShrink
    $table.Dock = [Windows.Forms.DockStyle]::Fill
    $table.Margin = New-Object Windows.Forms.Padding(0)
    $table.BackColor = $background
    $rows = New-Object System.Collections.Generic.List[object]

    $title = New-Object Windows.Forms.Label
    $title.Text = $Heading
    $title.Font = New-Object Drawing.Font('Segoe UI Semibold', 14)
    $title.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 10)
    $rows.Add($title)
    foreach ($paragraph in $Paragraphs) {
        $label = New-Object Windows.Forms.Label
        $label.Text = $paragraph
        $label.ForeColor = $muted
        $label.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 10)
        $rows.Add($label)
    }
    foreach ($label in $rows) {
        $label.AutoSize = $true  # with Dock Fill in the fixed-width column: wraps
        $label.Dock = [Windows.Forms.DockStyle]::Fill
        $label.UseMnemonic = $false  # an '&' is just text
    }
    $detailsBox = $null
    if ($Details) {
        # A borderless read-only text box: looks like a line of text, but can be selected and copied.
        $detailsBox = New-Object Windows.Forms.TextBox
        $detailsBox.Text = $Details
        $detailsBox.Font = New-Object Drawing.Font('Segoe UI', 9)
        $detailsBox.ForeColor = $faint
        $detailsBox.BackColor = $background
        $detailsBox.BorderStyle = [Windows.Forms.BorderStyle]::None
        $detailsBox.ReadOnly = $true
        $detailsBox.Multiline = $true
        $detailsBox.WordWrap = $true
        $detailsBox.TabStop = $false
        $detailsBox.Dock = [Windows.Forms.DockStyle]::Fill
        $detailsBox.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 10)
        $rows.Add($detailsBox)
    }

    $buttonRow = New-Object Windows.Forms.FlowLayoutPanel
    $buttonRow.AutoSize = $true
    $buttonRow.AutoSizeMode = [Windows.Forms.AutoSizeMode]::GrowAndShrink
    $buttonRow.WrapContents = $false
    $buttonRow.Anchor = [Windows.Forms.AnchorStyles]::Right
    $buttonRow.Margin = New-Object Windows.Forms.Padding(0, 12, 0, 0)
    $first = $true
    foreach ($spec in $Buttons) {
        $button = New-Object Windows.Forms.Button
        $button.Text = $spec.Text
        $button.DialogResult = [Windows.Forms.DialogResult]$spec.Result
        $button.Size = New-Object Drawing.Size(120, 36)
        $button.FlatStyle = [Windows.Forms.FlatStyle]::Flat
        $button.UseVisualStyleBackColor = $false
        $button.ForeColor = [Drawing.Color]::White
        if ($spec.Primary) {
            $button.BackColor = $accent
            $button.FlatAppearance.BorderSize = 0
            $button.FlatAppearance.MouseOverBackColor = $accentHover
            $button.FlatAppearance.MouseDownBackColor = $accentHover
        } else {
            $button.BackColor = $background
            $button.FlatAppearance.BorderColor = $accent
            $button.FlatAppearance.BorderSize = 1
            $button.FlatAppearance.MouseOverBackColor = $surface
            $button.FlatAppearance.MouseDownBackColor = $surface
        }
        $left = 12
        if ($first) { $left = 0 }
        $button.Margin = New-Object Windows.Forms.Padding($left, 0, 0, 0)
        $first = $false
        $buttonRow.Controls.Add($button)
        if ($spec.Result -eq $AcceptResult) { $form.AcceptButton = $button }
        if ($spec.Result -eq $CancelResult) { $form.CancelButton = $button }
    }
    $rows.Add($buttonRow)

    $table.RowCount = $rows.Count
    for ($row = 0; $row -lt $rows.Count; $row++) {
        [void]$table.RowStyles.Add((New-Object Windows.Forms.RowStyle([Windows.Forms.SizeType]::AutoSize)))
        $table.Controls.Add($rows[$row], 0, $row)
    }
    $form.Controls.Add($table)

    # Event handlers must not throw: an error there would end up in a Windows Forms error box.
    $form.Add_HandleCreated({
        param($sender, $eventArgs)
        Set-DarkTitleBar $sender.Handle
    })
    $form.Add_Shown({
        param($sender, $eventArgs)
        try {
            $sender.Activate()
            if ($sender.AcceptButton) { $sender.ActiveControl = [Windows.Forms.Control]$sender.AcceptButton }
        } catch {
            Write-Verbose ('Setup window not activated: ' + $_.Exception.Message)
        }
    })
    $form.ResumeLayout($false)  # scales everything to the screen's DPI
    $form.PerformLayout()

    if ($detailsBox) {
        # As many lines as the text box wraps the details into (at most 6, then it scrolls).
        $null = $detailsBox.Handle
        $lines = $detailsBox.GetLineFromCharIndex($detailsBox.TextLength) + 1
        if ($lines -gt 6) {
            $lines = 6
            $detailsBox.ScrollBars = [Windows.Forms.ScrollBars]::Vertical
        }
        $detailsBox.Height = $lines * $detailsBox.Font.Height + 2
        $form.PerformLayout()
    }
    return $form
}

function New-ProwlarrDialog {
    return New-SetupDialog -Heading 'Find more downloads?' -Paragraphs @(
        'AirGrabber can add Prowlarr, a free helper program that searches more websites for you. It runs quietly in the background.',
        'Windows will ask for permission once.'
    ) -Buttons @(
        @{ Text = 'No thanks'; Result = 'No' },
        @{ Text = 'Yes, add it'; Result = 'Yes'; Primary = $true }
    ) -AcceptResult 'Yes' -CancelResult 'No'
}

function Show-ProwlarrDialog {
    # The Prowlarr question: 'Yes', 'No' or 'Cancel' (the window's X), or $null when no window
    # could be shown.
    if (-not (Initialize-SetupWindow)) { return $null }
    $form = $null
    try {
        $form = New-ProwlarrDialog
        return [string]$form.ShowDialog()
    } catch {
        Write-Verbose ('No Prowlarr window: ' + $_.Exception.Message)
        return $null
    } finally {
        if ($form) { $form.Dispose() }
    }
}

function Get-ProwlarrPlan([bool]$Installed, [bool]$Wanted, [bool]$Declined) {
    # What to do about Prowlarr: 'installed' (already there), 'skip' (-NoProwlarr, which wins over
    # -Prowlarr), 'install' (-Prowlarr) or 'ask'.
    if ($Installed) { return 'installed' }
    if ($Declined) { return 'skip' }
    if ($Wanted) { return 'install' }
    return 'ask'
}

function Test-ProwlarrAnswer([string]$Answer) {
    # Only "Yes, add it" installs; No, Escape, the X and no window at all ($null) mean no.
    return ($Answer -eq 'Yes')
}

function Get-FailureText([Exception]$Exception) {
    # The failure window's explanation: a download or network error (anywhere in the exception's
    # chain), or anything else.
    $current = $Exception
    while ($current) {
        $type = $current.GetType().FullName
        if (@('System.Net.WebException', 'System.Net.Sockets.SocketException', 'System.Net.Http.HttpRequestException') -contains $type) {
            return 'Downloading failed. Check your internet connection and run the install command again.'
        }
        $current = $current.InnerException
    }
    return 'Something went wrong. Run the install command again; if it keeps failing, send the details below to whoever helped you set this up.'
}

function New-FailureDialog([Exception]$Exception) {
    return New-SetupDialog -Heading "AirGrabber couldn't be installed" -Paragraphs @(Get-FailureText $Exception) `
        -Details ('Details: ' + $Exception.Message) -Buttons @(
            @{ Text = 'Close'; Result = 'OK'; Primary = $true }
        ) -AcceptResult 'OK' -CancelResult 'OK'
}

function Show-FailureDialog([Exception]$Exception) {
    # $true when the failure window was shown (and closed), $false when no window could be shown.
    if (-not (Initialize-SetupWindow)) { return $false }
    $form = $null
    try {
        $form = New-FailureDialog $Exception
        $null = $form.ShowDialog()
        return $true
    } catch {
        Write-Verbose ('No failure window: ' + $_.Exception.Message)
        return $false
    } finally {
        if ($form) { $form.Dispose() }
    }
}

function Install-ProwlarrIfWanted {
    # Optional extra: a Prowlarr failure must never fail the AirGrabber install.
    $ErrorActionPreference = 'Continue'  # see Test-Python
    $installed = (Test-Path -LiteralPath 'C:\ProgramData\Prowlarr\config.xml') -or
        [bool](Get-Service -Name Prowlarr -ErrorAction SilentlyContinue)
    $plan = Get-ProwlarrPlan $installed $Prowlarr.IsPresent $NoProwlarr.IsPresent
    if ($plan -eq 'installed') {
        Write-Host '    The search helper (Prowlarr) is already installed; AirGrabber uses it automatically.'
        return
    }
    if ($plan -eq 'skip') { return }
    if ($plan -eq 'ask') {
        if (-not (Test-ProwlarrAnswer (Show-ProwlarrDialog))) { return }
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

    # winget installs Python and Prowlarr below; both have a fallback without it.
    $null = Initialize-Winget

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
    $failure = $_.Exception
    Write-Host ''
    Write-Host "Installation failed: $($failure.Message)" -ForegroundColor Red
    # A window says so too. Without one, keep the console window open so the message can be read (a
    # non-interactive host throws instead).
    $shown = $false
    try { $shown = Show-FailureDialog $failure } catch { $shown = $false }
    if (-not $shown) {
        try { $null = Read-Host 'Press Enter to close' } catch { }
    }
}
