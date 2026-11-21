# Try this folder's video player on one of your own videos, without touching AirGrabber's data.
# The desktop's "Try the player" shortcut runs this: drop a video on it, or double-click it and
# pick one. Its settings go to %TEMP%\AirGrabber-test (AIRGRABBER_DATA_DIR), not the real data.
# Windows PowerShell 5.1.

Add-Type -AssemblyName System.Windows.Forms
$project = Split-Path -Parent $PSScriptRoot

# An installed AirGrabber that is running would take the request and play it in its old player.
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike "*$project*" }
if ($running) {
    [System.Windows.Forms.MessageBox]::Show(
        "AirGrabber is still running. Quit it first (right-click its icon by the clock, then Quit), then try again.",
        "Try the player") | Out-Null
    return
}

$video = $args | Select-Object -First 1
if (-not $video) {
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Pick a video to try the player with"
    $dialog.Filter = "Videos|*.mkv;*.mp4;*.avi;*.m4v;*.mov;*.wmv;*.ts;*.webm|All files|*.*"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }
    $video = $dialog.FileName
}

$env:AIRGRABBER_DATA_DIR = Join-Path $env:TEMP "AirGrabber-test"
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    [System.Windows.Forms.MessageBox]::Show("Python wasn't found.", "Try the player") | Out-Null
    return
}
Start-Process -FilePath $python -ArgumentList @("main.py", "--play", "`"$video`"") -WorkingDirectory $project
