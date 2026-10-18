<#
AirGrabber uninstaller (Windows PowerShell 5.1). Windows' "Installed apps" runs it through the
UninstallString that install.ps1 registers:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<install>\app\uninstall.ps1"

It removes the install folder it lives in (this file's parent's parent), this install's Start menu,
desktop and Startup-folder (start with Windows) shortcuts and its "Installed apps" entry, and, when
the user agrees, the library and settings in %LOCALAPPDATA%\AirGrabber (or $env:AIRGRABBER_DATA_DIR).

-Quiet: no dialogs, and the library and settings are kept (testing and QuietUninstallString).

A folder without main.py, or with a tests folder (a development copy), is never removed.
No &&, ternaries or ?? operators (PowerShell 5.1).
#>
[CmdletBinding()]
param(
    [switch]$Quiet
)

$UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AirGrabber'
$Title = 'Uninstall AirGrabber'

function Normalize-Path([string]$Path) {
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Test-SamePath([string]$A, [string]$B) {
    if (-not $A -or -not $B) { return $false }
    try {
        return [string]::Equals((Normalize-Path $A), (Normalize-Path $B), [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Show-Dialog([string]$Text, [string]$Buttons = 'OK', [string]$Icon = 'Information', [string]$Default = 'Button1') {
    # Returns the button pressed ('Yes', 'No', 'Retry', ...); in -Quiet only writes the text.
    Write-Host $Text
    if ($Quiet) { return 'None' }
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true  # the uninstaller runs with a hidden window; keep dialogs in front
    try {
        $result = [System.Windows.Forms.MessageBox]::Show($owner, $Text, $Title,
            [System.Windows.Forms.MessageBoxButtons]$Buttons,
            [System.Windows.Forms.MessageBoxIcon]$Icon,
            [System.Windows.Forms.MessageBoxDefaultButton]$Default)
        return $result.ToString()
    } finally {
        $owner.Dispose()
    }
}

function Get-AppProcesses([string]$MainPy) {
    $found = @()
    $all = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $all) {
        $line = $process.CommandLine
        if ($line -and $line.IndexOf($MainPy, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $found += $process
        }
    }
    return $found
}

function Close-App([string]$MainPy) {
    # $true once no AirGrabber from this folder is running; $false when the user gives up.
    while ($true) {
        $running = @(Get-AppProcesses $MainPy)
        if ($running.Count -eq 0) { return $true }
        foreach ($item in $running) {
            $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
            if ($process) { $null = $process.CloseMainWindow() }
        }
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline -and @(Get-AppProcesses $MainPy).Count -gt 0) {
            Start-Sleep -Milliseconds 500
        }
        if (@(Get-AppProcesses $MainPy).Count -eq 0) { return $true }
        if ($Quiet) {
            Write-Host 'AirGrabber is still running, so it was not removed. Close it and try again.'
            return $false
        }
        $answer = Show-Dialog 'AirGrabber is still open. Close AirGrabber first, then press Retry.' 'RetryCancel' 'Warning'
        if ($answer -ne 'Retry') { return $false }
    }
}

function Get-FirstArgument([string]$Arguments) {
    # The text inside the first pair of quotes ('"<install>\main.py" --tray' gives
    # '<install>\main.py'), else the whole trimmed text. As first_argument in
    # app/services/app_identity.py.
    if (-not $Arguments) { return '' }
    $text = $Arguments.Trim()
    if (-not $text.StartsWith('"')) { return $text }
    $end = $text.IndexOf('"', 1)
    if ($end -lt 0) { return $text.Substring(1) }
    return $text.Substring(1, $end - 1)
}

function Remove-Shortcut([string]$Path, [string]$MainPy) {
    # Only removes a shortcut that starts this install (never one for another copy), whatever
    # options follow main.py (the Startup shortcut adds --tray).
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $arguments = $shell.CreateShortcut($Path).Arguments
    } catch {
        return
    }
    if (-not $arguments) { return }
    if (Test-SamePath (Get-FirstArgument $arguments) $MainPy) {
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    }
}

function Uninstall-AirGrabber {
    $ErrorActionPreference = 'Stop'
    if (-not $Quiet) { Add-Type -AssemblyName System.Windows.Forms }

    $installDir = Normalize-Path (Split-Path -Parent $PSScriptRoot)
    $mainPy = Join-Path $installDir 'main.py'
    if (-not (Test-Path -LiteralPath $mainPy -PathType Leaf)) {
        $null = Show-Dialog "This does not look like an AirGrabber installation, so nothing was removed:`n$installDir" 'OK' 'Warning'
        return
    }
    if (Test-Path -LiteralPath (Join-Path $installDir 'tests')) {
        $null = Show-Dialog "This is a development copy of AirGrabber, so nothing was removed:`n$installDir" 'OK' 'Warning'
        return
    }

    if (-not $Quiet) {
        $answer = Show-Dialog 'Uninstall AirGrabber?' 'YesNo' 'Question'
        if ($answer -ne 'Yes') { return }
    }

    if (-not (Close-App $mainPy)) { return }

    $dataDir = $env:AIRGRABBER_DATA_DIR
    if (-not $dataDir) { $dataDir = Join-Path $env:LOCALAPPDATA 'AirGrabber' }
    $removeData = $false
    if (-not $Quiet) {
        $answer = Show-Dialog "Also delete your library and settings? Your followed shows, download history and settings will be gone." 'YesNo' 'Question' 'Button2'
        $removeData = ($answer -eq 'Yes')
    }

    # Leave the folder before deleting it.
    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    [Environment]::CurrentDirectory = [IO.Path]::GetTempPath()

    Remove-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'AirGrabber.lnk') $mainPy
    Remove-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'AirGrabber.lnk') $mainPy
    # Start with Windows (app/services/startup.py): "<install>\main.py" --tray
    Remove-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'AirGrabber.lnk') $mainPy

    Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
    $leftOver = Test-Path -LiteralPath $installDir

    if (Test-Path -LiteralPath $UninstallKey) {
        $location = (Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue).InstallLocation
        if (Test-SamePath $location $installDir) {
            Remove-Item -LiteralPath $UninstallKey -Recurse -Force
        }
    }

    $dataLeftOver = $false
    if ($removeData -and (Test-Path -LiteralPath $dataDir)) {
        Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue
        $dataLeftOver = Test-Path -LiteralPath $dataDir
    }

    $message = 'AirGrabber was removed. Python and any helper programs (like Prowlarr or qBittorrent) are still installed; you can remove them under Installed apps.'
    if ($leftOver) {
        $message += "`n`nSome files could not be removed. You can delete this folder yourself:`n$installDir"
    }
    if ($dataLeftOver) {
        $message += "`n`nSome of your library and settings could not be removed. You can delete this folder yourself:`n$dataDir"
    }
    if ($Quiet) {
        Write-Host $message
    } else {
        $null = Show-Dialog $message 'OK' 'Information'
    }
}

try {
    Uninstall-AirGrabber
} catch {
    $null = Show-Dialog "AirGrabber could not be uninstalled: $($_.Exception.Message)" 'OK' 'Error'
}
