"""install.ps1 and app/uninstall.ps1 in Windows PowerShell 5.1: both parse, the installer's
shortcuts carry AirGrabber's AppUserModelID, the installer sets up winget when it is missing (found /
registered / downloaded / given up, with Get-Command, Add-AppxPackage and Invoke-WebRequest stubbed),
its Prowlarr and failure windows say and do what they should, it installs and sets up qBittorrent
only when Windows has no torrent program (registry, winget and AirGrabber's own Python stubbed),
and the uninstaller removes this install's shortcuts (the Startup one with its `--tray` included)
and nobody else's.

The scripts' functions are loaded from their syntax trees, so their main bodies never run; the
shortcuts and downloads live in a temporary folder, and no window is shown (the tests run
PowerShell with -NonInteractive).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.identity import APP_USER_MODEL_ID
from app.services.app_identity import first_argument
from app.services.powershell import NO_WINDOW, powershell_exe
from app.services.qbittorrent_setup import EXIT_CODES
from app.services.torrent_app import WINGET_COMMAND, command_executable
from app.models import QBittorrentSetupResult

PROJECT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT / "install.ps1"
UNINSTALLER = PROJECT / "app" / "uninstall.ps1"

# Defines the uninstaller's shortcut functions without running the uninstaller.
_LOAD_FUNCTIONS = """
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:AG_UNINSTALLER, [ref]$tokens, [ref]$errors)
$wanted = @('Normalize-Path', 'Test-SamePath', 'Get-FirstArgument', 'Remove-Shortcut')
$isFunction = { param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }
foreach ($function in $ast.FindAll($isFunction, $false)) {
    if ($wanted -contains $function.Name) { . ([scriptblock]::Create($function.Extent.Text)) }
}
function Out-Base64([string]$Text) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text)) }
"""


def _load_installer(functions: Sequence[str], constants: Sequence[str] = ()) -> str:
    """PowerShell defining these install.ps1 functions and constants (by name, e.g. '$AppId') from
    its syntax tree ($env:AG_INSTALLER); the installer itself never runs."""
    return f"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:AG_INSTALLER, [ref]$tokens, [ref]$errors)
$wantedFunctions = @({", ".join(f"'{name}'" for name in functions)})
$wantedConstants = @({", ".join(f"'{name}'" for name in constants)})
foreach ($statement in $ast.EndBlock.Statements) {{
    $isConstant = $statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $wantedConstants -contains $statement.Left.Extent.Text
    $isFunction = $statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $wantedFunctions -contains $statement.Name
    if ($isConstant -or $isFunction) {{ . ([scriptblock]::Create($statement.Extent.Text)) }}
}}
function Out-Base64([string]$Text) {{ [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text)) }}
"""


def _json_line(output: str) -> Any:
    """The value a script printed as 'JSON:' + base64 of UTF-8 JSON."""
    for line in output.splitlines():
        if line.startswith("JSON:"):
            return json.loads(base64.b64decode(line[5:]).decode())
    raise AssertionError(f"no JSON line in: {output}")


def _powershell(script: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        [powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
        creationflags=NO_WINDOW,
    )  # fmt: skip
    if result.returncode != 0 or result.stderr.strip():
        raise AssertionError(f"PowerShell failed ({result.returncode}): {result.stderr}")
    return result.stdout


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class ParseTests(unittest.TestCase):
    def test_both_scripts_parse_in_powershell_5_1(self) -> None:
        script = """
$ErrorActionPreference = 'Stop'
'version ' + $PSVersionTable.PSVersion.Major
foreach ($path in @($env:AG_INSTALLER, $env:AG_UNINSTALLER)) {
    $errors = $null
    $tokens = [System.Management.Automation.PSParser]::Tokenize([IO.File]::ReadAllText($path), [ref]$errors)
    $exits = @($tokens | Where-Object { $_.Type -eq 'Keyword' -and $_.Content -eq 'exit' }).Count
    (Split-Path -Leaf $path) + ' errors ' + $errors.Count + ' exits ' + $exits
    foreach ($e in $errors) { '  ' + $e.Message + ' (line ' + $e.Token.StartLine + ')' }
}
"""
        output = _powershell(script, {"AG_INSTALLER": str(INSTALLER), "AG_UNINSTALLER": str(UNINSTALLER)})
        lines = output.splitlines()
        self.assertEqual(lines[0], "version 5")
        # No `exit` in either: under `irm | iex` it would close the user's PowerShell window.
        self.assertEqual(lines[1:], ["install.ps1 errors 0 exits 0", "uninstall.ps1 errors 0 exits 0"])

    def test_the_language_parser_accepts_both(self) -> None:
        # The full parser (what `irm | iex` runs through), on the files; nothing is executed.
        script = """
$ErrorActionPreference = 'Stop'
foreach ($path in @($env:AG_INSTALLER, $env:AG_UNINSTALLER)) {
    $tokens = $null; $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    (Split-Path -Leaf $path) + ' ' + $errors.Count + ' ' + $PSVersionTable.PSVersion.Major
    foreach ($e in $errors) { '  ' + $e.Message + ' (line ' + $e.Extent.StartLineNumber + ')' }
}
"""
        output = _powershell(script, {"AG_INSTALLER": str(INSTALLER), "AG_UNINSTALLER": str(UNINSTALLER)})
        self.assertEqual(output.splitlines(), ["install.ps1 0 5", "uninstall.ps1 0 5"])

    def test_the_installer_is_ascii(self) -> None:
        # Windows PowerShell 5.1 reads a script file without a BOM in the ANSI code page.
        self.assertTrue(INSTALLER.read_bytes().isascii())

    def test_every_winget_install_uses_wingets_own_source(self) -> None:
        # Without it a failing Microsoft Store source (e.g. its certificate check) made winget
        # ask which source to use instead of installing Python / qBittorrent / Prowlarr.
        text = INSTALLER.read_text(encoding="ascii")
        installs = [line for line in text.splitlines() if "$winget.Source install" in line]
        self.assertEqual(len(installs), 3)
        for line in installs:
            self.assertIn(" --exact --source winget ", line)


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class InstallerShortcutTests(unittest.TestCase):
    def test_new_shortcut_carries_the_app_id(self) -> None:
        """install.ps1's New-Shortcut (Start menu and desktop), into a temporary folder."""
        # Its constants and two functions from the syntax tree; the installer itself never runs.
        script = """
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:AG_INSTALLER, [ref]$tokens, [ref]$errors)
foreach ($statement in $ast.EndBlock.Statements) {
    $isConstant = $statement -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        @('$IconPath', '$AppId', '$AppIdHelper') -contains $statement.Left.Extent.Text
    $isFunction = $statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        @('New-Shortcut', 'Set-ShortcutAppId') -contains $statement.Name
    if ($isConstant -or $isFunction) { . ([scriptblock]::Create($statement.Extent.Text)) }
}
$InstallDir = $env:AG_PROJECT  # has app\\shortcut_app_id.cs, as an installed copy does
$link = Join-Path $env:AG_ROOT 'AirGrabber.lnk'
New-Shortcut $link (Join-Path $env:SystemRoot 'notepad.exe')
'ARGUMENTS ' + (New-Object -ComObject WScript.Shell).CreateShortcut($link).Arguments
'APP_ID ' + [AirGrabber.ShortcutAppId]::Get($link)
"""
        with tempfile.TemporaryDirectory() as temp:
            output = _powershell(
                script,
                {"AG_INSTALLER": str(INSTALLER), "AG_PROJECT": str(PROJECT), "AG_ROOT": temp},
            )
        self.assertEqual(
            output.splitlines(),
            [f'ARGUMENTS "{PROJECT}\\main.py"', f"APP_ID {APP_USER_MODEL_ID}"],
        )


# winget's DesktopAppInstaller_Dependencies.zip (v1.29.380): the frameworks per processor.
_FRAMEWORKS = (
    "Microsoft.VCLibs.140.00_14.0.33519.0_{arch}.appx",
    "Microsoft.VCLibs.140.00.UWPDesktop_14.0.33728.0_{arch}.appx",
    "Microsoft.WindowsAppRuntime.1.8_8000.616.304.0_{arch}.appx",
)
_WINGET_FUNCTIONS = (
    "Write-Step", "Add-WindowsAppsToPath", "Test-Winget", "Wait-Winget", "Get-WingetArchitecture",
    "Install-WingetFromGitHub", "Initialize-Winget",
)  # fmt: skip
_APP_INSTALLER = "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe"
_RELEASE = "https://github.com/microsoft/winget-cli/releases/download/v1.29.380"
_GAVE_UP = "Couldn't add winget; continuing without it."

# Runs Initialize-Winget once per scenario ($env:AG_SCENARIOS) against stubs, never the real
# Get-Command / Add-AppxPackage / Invoke-WebRequest / Start-Sleep, and prints what happened as JSON.
_WINGET_SCENARIOS = """
function Get-Command { if ($global:AgWinget) { [pscustomobject]@{ Source = 'C:\\stub\\winget.exe' } } }
function Start-Sleep { }
function Add-AppxPackage {
    param([string]$Path, [switch]$RegisterByFamilyName, [string]$MainPackage)
    if ($RegisterByFamilyName) {
        $global:AgLog.Add('register ' + $MainPackage)
        if ($global:AgScenario.register -eq 'winget') { $global:AgWinget = $true; return }
        if ($global:AgScenario.register -eq 'old') { return }  # an App Installer without winget
        throw 'stub: App Installer is not on this PC'
    }
    if (-not (Test-Path -LiteralPath $Path)) { throw ('stub: no file ' + $Path) }
    $name = Split-Path -Leaf $Path
    $global:AgLog.Add('add ' + $name)
    if ($name -like '*.msixbundle') {
        if ($global:AgScenario.bundle -eq 'fail') { throw 'stub: App Installer could not be added' }
        $global:AgWinget = $true
        return
    }
    if ($name -like 'Microsoft.VCLibs.140.00_*') { throw 'stub: a higher version is already installed' }
}
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)
    $global:AgLog.Add('get ' + $Uri)
    if ($global:AgScenario.offline) { throw (New-Object System.Net.WebException 'stub: no internet') }
    if ($Uri -like '*.zip') {
        Copy-Item -LiteralPath $env:AG_DEPENDENCIES -Destination $OutFile
    } else {
        [IO.File]::WriteAllText($OutFile, 'stub bundle')
    }
}

$results = New-Object System.Collections.Generic.List[object]
foreach ($scenario in (ConvertFrom-Json $env:AG_SCENARIOS)) {
    $global:AgScenario = $scenario
    $global:AgWinget = [bool]$scenario.winget
    $global:AgLog = New-Object System.Collections.Generic.List[string]
    $env:LOCALAPPDATA = $env:AG_LOCALAPPDATA
    $env:PATH = $scenario.path
    $env:PROCESSOR_ARCHITECTURE = $scenario.arch
    Remove-Item Env:PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue
    # Write-Host goes to the information stream (6): host lines and output kept apart.
    $records = @(Initialize-Winget 6>&1)
    # [string[]]: Windows PowerShell's ConvertTo-Json fails on nested pipeline-made arrays.
    $results.Add([pscustomobject]@{
        name = [string]$scenario.name
        output = [string[]]@($records | Where-Object { $_ -isnot [System.Management.Automation.InformationRecord] } |
            ForEach-Object { [string]$_ })
        host = [string[]]@($records | Where-Object { $_ -is [System.Management.Automation.InformationRecord] } |
            ForEach-Object { [string]$_.MessageData })
        log = [string[]]$global:AgLog.ToArray()
        path = [string]$env:PATH
        leftovers = [string[]]@(Get-ChildItem -LiteralPath $env:AG_TEMP -Filter 'airgrabber-winget-*' |
            ForEach-Object { $_.Name })
    })
}
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $results.ToArray() -Depth 4 -Compress))
"""


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class InstallerWingetTests(unittest.TestCase):
    """Initialize-Winget: found, registered, downloaded from winget's release, or given up."""

    apps: str
    results: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        cls.apps = str(root / "LocalAppData" / "Microsoft" / "WindowsApps")
        dependencies = root / "DesktopAppInstaller_Dependencies.zip"
        with zipfile.ZipFile(dependencies, "w") as archive:
            for arch in ("arm64", "x64", "x86"):
                for framework in _FRAMEWORKS:
                    archive.writestr(f"{arch}/{framework.format(arch=arch)}", b"stub framework")
        downloads = root / "temp"
        downloads.mkdir()
        system = r"C:\Windows\System32"
        base = {"winget": False, "register": "fail", "offline": False, "bundle": "", "arch": "AMD64", "path": system}
        scenarios = [
            {**base, "name": "found", "winget": True},
            {**base, "name": "on_path", "winget": True, "path": f"{system};{cls.apps.upper()}\\"},
            {**base, "name": "registered", "register": "winget"},
            {**base, "name": "downloaded"},
            {**base, "name": "downloaded_arm64", "register": "old", "arch": "ARM64"},
            {**base, "name": "offline", "offline": True},
            {**base, "name": "bundle_refused", "bundle": "fail"},
        ]
        script = _load_installer(_WINGET_FUNCTIONS, ("$WingetRelease", "$AppInstallerFamily")) + _WINGET_SCENARIOS
        output = _powershell(
            script,
            {
                "AG_INSTALLER": str(INSTALLER),
                "AG_SCENARIOS": json.dumps(scenarios),
                "AG_DEPENDENCIES": str(dependencies),
                "AG_LOCALAPPDATA": str(root / "LocalAppData"),
                "AG_TEMP": str(downloads),
                "TEMP": str(downloads),
                "TMP": str(downloads),
            },
        )
        cls.results = {result["name"]: result for result in _json_line(output)}

    def _assert_downloaded(self, log: list[str], arch: str) -> None:
        self.assertEqual(
            log[:3],
            [
                f"register {_APP_INSTALLER}",
                f"get {_RELEASE}/DesktopAppInstaller_Dependencies.zip",
                f"get {_RELEASE}/{_APP_INSTALLER}.msixbundle",
            ],
        )
        # This processor's frameworks only, both VCLibs before the Windows App Runtime, App
        # Installer last. A framework that will not install (a newer one is there) stops nothing.
        frameworks = [f"add {name.format(arch=arch)}" for name in _FRAMEWORKS]
        self.assertCountEqual(log[3:5], frameworks[:2])
        self.assertEqual(log[5:], [frameworks[2], f"add {_APP_INSTALLER}.msixbundle"])

    def test_winget_found_needs_nothing(self) -> None:
        result = self.results["found"]
        self.assertEqual((result["output"], result["log"], result["host"]), (["found"], [], []))
        # The folder of winget's app alias is on PATH for this session.
        self.assertEqual(result["path"], rf"C:\Windows\System32;{self.apps}")

    def test_windows_apps_is_not_added_twice(self) -> None:
        result = self.results["on_path"]
        self.assertEqual(result["output"], ["found"])
        self.assertEqual(result["path"], rf"C:\Windows\System32;{self.apps.upper()}" + "\\")

    def test_registers_the_app_installer_windows_has(self) -> None:
        result = self.results["registered"]
        self.assertEqual(result["output"], ["registered"])
        self.assertEqual(result["log"], [f"register {_APP_INSTALLER}"])
        self.assertEqual(result["host"], ["==> Getting Windows' app installer (winget)..."])

    def test_downloads_it_when_windows_has_none(self) -> None:
        result = self.results["downloaded"]
        self.assertEqual(result["output"], ["downloaded"])
        self._assert_downloaded(result["log"], "x64")
        self.assertEqual(result["leftovers"], [])  # the downloads are removed
        self.assertNotIn(_GAVE_UP, result["host"])

    def test_arm_pcs_get_arm64_frameworks(self) -> None:
        # Registered, but that App Installer has no winget: downloaded after all.
        result = self.results["downloaded_arm64"]
        self.assertEqual(result["output"], ["downloaded"])
        self._assert_downloaded(result["log"], "arm64")

    def test_offline_carries_on_without_winget(self) -> None:
        result = self.results["offline"]
        self.assertEqual(result["output"], ["failed"])
        self.assertEqual(
            result["log"], [f"register {_APP_INSTALLER}", f"get {_RELEASE}/DesktopAppInstaller_Dependencies.zip"]
        )
        self.assertEqual(result["host"][-1], _GAVE_UP)
        self.assertEqual(result["leftovers"], [])

    def test_a_refused_app_installer_carries_on_without_winget(self) -> None:
        result = self.results["bundle_refused"]
        self.assertEqual(result["output"], ["failed"])
        self._assert_downloaded(result["log"], "x64")
        self.assertEqual(result["host"][-1], _GAVE_UP)
        self.assertEqual(result["leftovers"], [])

    def test_winget_comes_before_python(self) -> None:
        source = INSTALLER.read_text(encoding="ascii")
        body = source[source.index("function Install-AirGrabber {") :]
        self.assertLess(body.index("$null = Initialize-Winget"), body.index("Write-Step 'Checking for Python'"))


_DOWNLOAD_TEXT = "Downloading failed. Check your internet connection and run the install command again."
_OTHER_TEXT = (
    "Something went wrong. Run the install command again; if it keeps failing, send the details below "
    "to whoever helped you set this up."
)
_DIALOG_FUNCTIONS = (
    "Initialize-SetupWindow", "Set-DarkTitleBar", "New-SetupDialog", "New-ProwlarrDialog",
    "Show-ProwlarrDialog", "Get-ProwlarrPlan", "Test-ProwlarrAnswer", "Get-FailureText",
    "New-FailureDialog", "Show-FailureDialog",
)  # fmt: skip

# Both windows built, never shown; sizes back at 100 % whatever this PowerShell's DPI.
_DESCRIBE_WINDOWS = """
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
function Get-Html($color) { '#{0:X2}{1:X2}{2:X2}' -f $color.R, $color.G, $color.B }
function Get-Window($form) {
    $scale = $form.CurrentAutoScaleDimensions.Width / 96
    $table = $form.Controls[0]
    $buttons = @($table.Controls | Where-Object { $_ -is [Windows.Forms.FlowLayoutPanel] } |
        ForEach-Object { $_.Controls })
    [pscustomobject]@{
        title = $form.Text
        background = Get-Html $form.BackColor
        fixed = ($form.FormBorderStyle -eq 'FixedDialog') -and -not $form.MinimizeBox -and -not $form.MaximizeBox
        topMost = $form.TopMost
        autoScale = [string]$form.AutoScaleMode
        width = [int][Math]::Round($form.ClientSize.Width / $scale)
        texts = @($table.Controls | Where-Object { $_ -isnot [Windows.Forms.FlowLayoutPanel] } | ForEach-Object {
            $_.Text + ' | ' + $_.Font.Name + ' ' + $_.Font.SizeInPoints + ' | ' + (Get-Html $_.ForeColor)
        })
        readOnly = @($table.Controls | Where-Object { $_ -is [Windows.Forms.TextBox] } | ForEach-Object { $_.ReadOnly })
        buttons = @($buttons | ForEach-Object {
            $_.Text + ' | ' + $_.DialogResult + ' | ' + $_.FlatStyle + ' | ' + (Get-Html $_.BackColor) + ' | ' +
                (Get-Html $_.FlatAppearance.BorderColor) + ' ' + $_.FlatAppearance.BorderSize + ' | ' +
                [int][Math]::Round($_.Width / $scale) + 'x' + [int][Math]::Round($_.Height / $scale)
        })
        accept = $form.AcceptButton.Text
        cancel = $form.CancelButton.Text
    }
    $form.Dispose()
}
$windows = @(
    (Get-Window (New-ProwlarrDialog)),
    (Get-Window (New-FailureDialog (New-Object System.Net.WebException 'No internet & no luck')))
)
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $windows -Depth 3 -Compress))
"""


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class InstallerWindowTests(unittest.TestCase):
    """The Prowlarr question and the failure report: their pure logic, the windows' contents (built,
    never shown), and the fallback when no window can be shown."""

    def _run(self, script: str, env: dict[str, str] | None = None) -> Any:
        environment = {"AG_INSTALLER": str(INSTALLER), **(env or {})}
        return _json_line(_powershell(_load_installer(_DIALOG_FUNCTIONS) + script, environment))

    def test_prowlarr_plan(self) -> None:
        # (already installed, -Prowlarr, -NoProwlarr) -> plan
        cases = {
            (True, False, False): "installed",
            (True, True, True): "installed",
            (False, False, True): "skip",
            (False, True, True): "skip",  # -NoProwlarr wins
            (False, True, False): "install",
            (False, False, False): "ask",
        }
        plans = self._run(
            """
$plans = foreach ($case in (ConvertFrom-Json $env:AG_CASES)) { Get-ProwlarrPlan $case[0] $case[1] $case[2] }
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject @($plans) -Compress))
""",
            {"AG_CASES": json.dumps(list(cases))},
        )
        self.assertEqual(plans, list(cases.values()))

    def test_only_yes_installs_prowlarr(self) -> None:
        answers = self._run("""
$answers = foreach ($answer in @('Yes', 'No', 'Cancel', '', $null)) { Test-ProwlarrAnswer $answer }
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject @($answers) -Compress))
""")
        self.assertEqual(answers, [True, False, False, False, False])

    def test_failure_text_by_error(self) -> None:
        texts = self._run("""
Add-Type -AssemblyName System.Net.Http
$web = New-Object System.Net.WebException 'The remote name could not be resolved'
$errors = @(
    $web,
    (New-Object System.Management.Automation.RuntimeException 'Download stopped', $web),
    (New-Object System.Net.Sockets.SocketException 10060),
    (New-Object System.Net.Http.HttpRequestException 'An error occurred while sending the request.'),
    (New-Object System.Management.Automation.RuntimeException 'Python could not be installed (error code 1603).'),
    (New-Object System.IO.IOException 'The download looks damaged. Please try again.')
)
$texts = foreach ($exception in $errors) { Get-FailureText $exception }
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject @($texts) -Compress))
""")
        self.assertEqual(texts, [_DOWNLOAD_TEXT] * 4 + [_OTHER_TEXT] * 2)

    def test_no_window_means_no_and_the_console_fallback(self) -> None:
        # The tests' PowerShell runs with -NonInteractive: nothing may be shown or wait for a click.
        started = time.monotonic()
        result = self._run("""
$prowlarr = Show-ProwlarrDialog
$failure = Show-FailureDialog (New-Object System.Exception 'boom')
$result = [pscustomobject]@{
    ready = Initialize-SetupWindow
    prowlarr = $prowlarr
    wants = Test-ProwlarrAnswer $prowlarr
    failureShown = $failure
}
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $result -Compress))
""")
        self.assertEqual(result, {"ready": False, "prowlarr": None, "wants": False, "failureShown": False})
        self.assertLess(time.monotonic() - started, 60)

    def test_the_windows_say_what_they_should(self) -> None:
        prowlarr, failure = self._run(_DESCRIBE_WINDOWS)
        common = {"title": "AirGrabber setup", "background": "#161616", "fixed": True, "topMost": True,
                  "autoScale": "Dpi", "width": 480}  # fmt: skip
        self.assertEqual(
            prowlarr,
            {
                **common,
                "texts": [
                    "Find more downloads? | Segoe UI Semibold 14 | #FFFFFF",
                    "AirGrabber can add Prowlarr, a free helper program that searches more websites for you. "
                    "It runs quietly in the background. | Segoe UI 10.5 | #A4B2C6",
                    "Windows will ask for permission once. | Segoe UI 10.5 | #A4B2C6",
                ],
                "readOnly": [],
                "buttons": [
                    "No thanks | No | Flat | #161616 | #4E6A85 1 | 120x36",
                    "Yes, add it | Yes | Flat | #4E6A85 | #000000 0 | 120x36",
                ],
                "accept": "Yes, add it",  # Enter
                "cancel": "No thanks",  # Escape; the X closes with Cancel, also a no
            },
        )
        self.assertEqual(
            failure,
            {
                **common,
                "texts": [
                    "AirGrabber couldn't be installed | Segoe UI Semibold 14 | #FFFFFF",
                    f"{_DOWNLOAD_TEXT} | Segoe UI 10.5 | #A4B2C6",
                    "Details: No internet & no luck | Segoe UI 9 | #8A8A8A",
                ],
                "readOnly": [True],  # selectable, so the details can be copied
                "buttons": ["Close | OK | Flat | #4E6A85 | #000000 0 | 120x36"],
                "accept": "Close",
                "cancel": "Close",
            },
        )

    def test_the_failure_window_comes_before_the_console_prompt(self) -> None:
        source = INSTALLER.read_text(encoding="ascii")
        main = source[source.rindex("try {\n    Install-AirGrabber") :]
        self.assertIn("$shown = Show-FailureDialog $failure", main)
        self.assertIn("if (-not $shown) {\n        try { $null = Read-Host 'Press Enter to close' } catch { }", main)

_QBIT_FUNCTIONS = (
    "Write-Step", "Get-RegistryString", "Get-OpenCommandExe", "Find-TorrentApp", "Get-QBittorrentPlan",
    "Install-QBittorrentIfNeeded",
)  # fmt: skip
_CLASSES = "Registry::HKEY_CLASSES_ROOT"
_MAGNET_CHOICE = r"HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
_TORRENT_CHOICE = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice"
_INSTALL_DIR = r"C:\Users\Alex\AppData\Local\Programs\AirGrabber"
_QBIT_EXE = r"C:\Program Files\qBittorrent\qbittorrent.exe"

# Find-TorrentApp per scenario against a stubbed registry ("path|value name" -> string); the
# programs' files are real ones in a temporary folder.
_FIND_SCENARIOS = """
function Get-RegistryString([string]$Path, [string]$Name) {
    $property = $global:AgScenario.registry.PSObject.Properties[$Path + '|' + $Name]
    if ($property) { return [string]$property.Value }
    return $null
}
$results = New-Object System.Collections.Generic.List[object]
foreach ($scenario in (ConvertFrom-Json $env:AG_SCENARIOS)) {
    $global:AgScenario = $scenario
    $results.Add([pscustomobject]@{ name = [string]$scenario.name; found = [string](Find-TorrentApp) })
}
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $results.ToArray() -Compress))
"""

# Install-QBittorrentIfNeeded per scenario: Find-TorrentApp answers in turn, winget and
# AirGrabber's Python are stub functions that log their arguments and set an exit code.
_INSTALL_SCENARIOS = """
function Find-TorrentApp {
    $answer = $null
    if ($global:AgFinds.Count -gt 0) { $answer = $global:AgFinds[0]; $global:AgFinds.RemoveAt(0) }
    if ($answer) { return $answer }
    return $null
}
function Get-Command { if ($global:AgScenario.winget -ne $null) { [pscustomobject]@{ Source = 'Fake-Winget' } } }
function Fake-Winget { $global:AgLog.Add('winget ' + ($args -join ' ')); $global:LASTEXITCODE = [int]$global:AgScenario.winget }
function Fake-Python { $global:AgLog.Add('python ' + ($args -join ' ')); $global:LASTEXITCODE = [int]$global:AgScenario.python }
$InstallDir = $env:AG_INSTALL_DIR
$results = New-Object System.Collections.Generic.List[object]
foreach ($scenario in (ConvertFrom-Json $env:AG_SCENARIOS)) {
    $global:AgScenario = $scenario
    $global:AgFinds = New-Object System.Collections.Generic.List[string]
    foreach ($answer in @($scenario.finds)) { $global:AgFinds.Add([string]$answer) }
    $global:AgLog = New-Object System.Collections.Generic.List[string]
    $NoQBittorrent = [switch][bool]$scenario.declined
    $records = @(Install-QBittorrentIfNeeded 'Fake-Python' 6>&1)
    $results.Add([pscustomobject]@{
        name = [string]$scenario.name
        host = [string[]]@($records | Where-Object { $_ -is [System.Management.Automation.InformationRecord] } |
            ForEach-Object { [string]$_.MessageData })
        log = [string[]]$global:AgLog.ToArray()
    })
}
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $results.ToArray() -Depth 4 -Compress))
"""

_INSTALL_LINE = "==> Installing qBittorrent, the free program AirGrabber downloads with (Windows may ask for permission)"
_READY = "    qBittorrent is installed and ready: downloads start without a pop-up."
_NOT_INSTALLED = "qBittorrent couldn't be installed automatically. AirGrabber offers it again when it starts."


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class InstallerQBittorrentTests(unittest.TestCase):
    """qBittorrent in the installer: only when Windows has no program for torrents, fully
    automatic (no question window), set up by AirGrabber's `--setup-qbittorrent`, never failing
    the AirGrabber install."""

    results: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        base = {"finds": [], "winget": 0, "python": 0, "declined": False}
        scenarios = [
            {**base, "name": "installed"},
            {**base, "name": "present", "finds": [r"C:\Program Files\Deluge\deluge.exe"]},
            {**base, "name": "declined", "declined": True},
            {**base, "name": "running", "python": EXIT_CODES[QBittorrentSetupResult.RUNNING]},
            {**base, "name": "setup_failed", "python": EXIT_CODES[QBittorrentSetupResult.FAILED]},
            {**base, "name": "winget_failed", "winget": 1},
            {**base, "name": "no_winget", "winget": None},
            # qBittorrent was on the PC without the torrent links: winget has nothing to do.
            {**base, "name": "there_but_not_linked", "winget": -1978335189, "finds": ["", _QBIT_EXE]},
        ]
        output = _powershell(
            _load_installer(_QBIT_FUNCTIONS) + _INSTALL_SCENARIOS,
            {"AG_INSTALLER": str(INSTALLER), "AG_SCENARIOS": json.dumps(scenarios), "AG_INSTALL_DIR": _INSTALL_DIR},
        )
        cls.results = {result["name"]: result for result in _json_line(output)}

    def test_installs_and_sets_up_qbittorrent_without_asking(self) -> None:
        result = self.results["installed"]
        self.assertEqual(
            result["log"],
            [
                "winget " + " ".join(WINGET_COMMAND),  # the same command as the app's Install
                rf"python {_INSTALL_DIR}\main.py --setup-qbittorrent",
            ],
        )
        self.assertEqual(result["host"], [_INSTALL_LINE, _READY])

    def test_a_torrent_program_already_there_is_left_alone(self) -> None:
        result = self.results["present"]
        self.assertEqual(result["log"], [])
        self.assertEqual(result["host"], ["    A torrent program is already installed; AirGrabber uses it."])

    def test_no_qbittorrent_skips_it(self) -> None:
        self.assertEqual((self.results["declined"]["log"], self.results["declined"]["host"]), ([], []))

    def test_qbittorrent_running_or_a_failed_setup_is_said_plainly(self) -> None:
        running = self.results["running"]
        self.assertEqual(len(running["log"]), 2)
        self.assertEqual(
            running["host"][-1],
            "    qBittorrent is installed. Close qBittorrent, then set it up in AirGrabber (Settings, "
            "Where downloads go).",
        )
        self.assertEqual(
            self.results["setup_failed"]["host"][-1],
            "    qBittorrent is installed. Set it up in AirGrabber (Settings, Where downloads go).",
        )

    def test_a_failed_install_carries_on(self) -> None:
        failed = self.results["winget_failed"]
        self.assertEqual(failed["log"], ["winget " + " ".join(WINGET_COMMAND)])  # no setup
        self.assertEqual(failed["host"], [_INSTALL_LINE, _NOT_INSTALLED])
        no_winget = self.results["no_winget"]
        self.assertEqual((no_winget["log"], no_winget["host"]), ([], [_INSTALL_LINE, _NOT_INSTALLED]))

    def test_qbittorrent_there_already_is_set_up(self) -> None:
        result = self.results["there_but_not_linked"]
        self.assertEqual(result["log"][-1], rf"python {_INSTALL_DIR}\main.py --setup-qbittorrent")
        self.assertEqual(result["host"][-1], _READY)

    def test_plan(self) -> None:
        script = _load_installer(_QBIT_FUNCTIONS) + """
$plans = @()
foreach ($case in @(@($false, $false), @($true, $false), @($false, $true), @($true, $true))) {
    $plans += Get-QBittorrentPlan $case[0] $case[1]
}
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $plans -Compress))
"""
        plans = _json_line(_powershell(script, {"AG_INSTALLER": str(INSTALLER)}))
        # (a torrent program found, -NoQBittorrent) -> plan
        self.assertEqual(plans, ["install", "present", "skip", "skip"])

    def test_it_runs_after_python_and_the_app_and_before_prowlarr(self) -> None:
        source = INSTALLER.read_text(encoding="ascii")
        body = source[source.index("function Install-AirGrabber {") :]
        self.assertLess(body.index("    Install-App\n"), body.index("Install-QBittorrentIfNeeded $python"))
        self.assertLess(body.index("-r (Join-Path $InstallDir 'requirements.txt')"), body.index("Install-QBittorrentIfNeeded"))
        self.assertLess(body.index("Install-QBittorrentIfNeeded"), body.index("Install-ProwlarrIfWanted"))
        self.assertIn("[switch]$NoQBittorrent", source)
        self.assertIn("-NoQBittorrent skips that", source)  # documented in the header

    def test_finds_the_torrent_program_like_the_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            qbit = Path(temp, "qBittorrent", "qbittorrent.exe")
            deluge = Path(temp, "Deluge", "deluge.exe")
            for exe in (qbit, deluge):
                exe.parent.mkdir()
                exe.write_bytes(b"stub")
            missing = Path(temp, "Gone", "utorrent.exe")
            magnet = rf"{_CLASSES}\magnet\shell\open\command|"
            scenarios = [
                {"name": "none", "registry": {}},
                {"name": "magnet", "registry": {magnet: f'"{qbit}" "%1"'}},
                {
                    "name": "user_choice_first",
                    "registry": {
                        f"{_MAGNET_CHOICE}|ProgId": "Deluge.Magnet",
                        rf"{_CLASSES}\Deluge.Magnet\shell\open\command|": f'"{deluge}" "%1"',
                        magnet: f'"{qbit}" "%1"',
                    },
                },
                {
                    "name": "missing_then_torrent_file",
                    "registry": {
                        magnet: f'"{missing}" "%1"',
                        rf"{_CLASSES}\.torrent|": "qBittorrent.File.Torrent",
                        rf"{_CLASSES}\qBittorrent.File.Torrent\shell\open\command|": f"{qbit} %1",
                    },
                },
                {
                    "name": "torrent_user_choice",
                    "registry": {
                        f"{_TORRENT_CHOICE}|ProgId": "Applications\\deluge.exe",
                        rf"{_CLASSES}\Applications\deluge.exe\shell\open\command|": f'"{deluge}" "%1"',
                    },
                },
                {"name": "only_missing", "registry": {magnet: f'"{missing}" "%1"'}},
            ]
            output = _powershell(
                _load_installer(_QBIT_FUNCTIONS) + _FIND_SCENARIOS,
                {"AG_INSTALLER": str(INSTALLER), "AG_SCENARIOS": json.dumps(scenarios)},
            )
            found = {result["name"]: result["found"] for result in _json_line(output)}
        self.assertEqual(
            found,
            {
                "none": "",
                "magnet": str(qbit),
                "user_choice_first": str(deluge),
                "missing_then_torrent_file": str(qbit),
                "torrent_user_choice": str(deluge),
                "only_missing": "",
            },
        )

    def test_reads_the_program_from_an_open_command_like_the_app(self) -> None:
        commands = [
            f'"{_QBIT_EXE}" "%1"',
            r"C:\Program Files\qBittorrent\qbittorrent.exe %1",
            r'%ProgramFiles%\Deluge\deluge.exe "%1"',
            r"C:\Tools\tixati.exe",
            "rundll32 url.dll,FileProtocolHandler %1",
        ]
        script = _load_installer(_QBIT_FUNCTIONS) + """
$exes = @(foreach ($command in (ConvertFrom-Json $env:AG_COMMANDS)) { [string](Get-OpenCommandExe $command) })
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $exes -Compress))
"""
        exes = _json_line(_powershell(script, {"AG_INSTALLER": str(INSTALLER), "AG_COMMANDS": json.dumps(commands)}))
        self.assertEqual(exes, [command_executable(command) for command in commands])

    def test_reads_a_registry_string_or_nothing(self) -> None:
        # Read-only: a value every Windows has, and keys that don't exist.
        script = _load_installer(_QBIT_FUNCTIONS) + """
$values = @(
    [string](Get-RegistryString 'Registry::HKEY_CLASSES_ROOT\\.txt' ''),
    [string](Get-RegistryString 'Registry::HKEY_CLASSES_ROOT\\AirGrabber.NoSuchKey' ''),
    [string](Get-RegistryString 'HKCU:\\Software\\AirGrabber-NoSuchKey' 'ProgId')
)
'JSON:' + (Out-Base64 (ConvertTo-Json -InputObject $values -Compress))
"""
        txt, missing_class, missing_user = _json_line(_powershell(script, {"AG_INSTALLER": str(INSTALLER)}))
        self.assertTrue(txt)
        self.assertEqual((missing_class, missing_user), ("", ""))


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell is Windows-only")
class UninstallerShortcutTests(unittest.TestCase):
    def test_get_first_argument_matches_python(self) -> None:
        cases = [
            '"C:\\Air Grabber\\main.py"',
            '"C:\\Air Grabber\\main.py" --tray',
            '  "C:\\Air Grabber\\main.py"   --tray --more ',
            '"C:\\Air Grabber\\main.py',
            "C:\\AirGrabber\\main.py",
            "",
        ]
        script = _LOAD_FUNCTIONS + """
$cases = ConvertFrom-Json $env:AG_CASES
foreach ($case in $cases) { 'R:' + (Out-Base64 (Get-FirstArgument $case)) }
"""
        output = _powershell(
            script, {"AG_UNINSTALLER": str(UNINSTALLER), "AG_CASES": json.dumps(cases)}
        )
        results = [
            base64.b64decode(line[2:]).decode() for line in output.splitlines() if line.startswith("R:")
        ]
        self.assertEqual(results, [first_argument(case) for case in cases])

    def test_removes_only_this_installs_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "Air Grabber"
            other = root / "Air Grabber 2"
            links = {
                "startup_tray.lnk": f'"{install}\\main.py" --tray',  # Start with Windows
                "start_menu.lnk": f'"{install}\\main.py"',
                "other_tray.lnk": f'"{other}\\main.py" --tray',
                "other.lnk": f'"{other}\\main.py"',
                "no_arguments.lnk": "",
            }
            script = _LOAD_FUNCTIONS + """
$shell = New-Object -ComObject WScript.Shell
$links = ConvertFrom-Json $env:AG_LINKS
foreach ($name in $links.PSObject.Properties.Name) {
    $link = $shell.CreateShortcut((Join-Path $env:AG_ROOT $name))
    $link.TargetPath = $env:SystemRoot + '\\notepad.exe'
    $link.Arguments = $links.$name
    $link.Save()
}
foreach ($name in $links.PSObject.Properties.Name) {
    Remove-Shortcut (Join-Path $env:AG_ROOT $name) (Join-Path $env:AG_INSTALL 'main.py')
}
"""
            _powershell(
                script,
                {
                    "AG_UNINSTALLER": str(UNINSTALLER),
                    "AG_ROOT": str(root),
                    "AG_INSTALL": str(install),
                    "AG_LINKS": json.dumps(links),
                },
            )
            left = sorted(path.name for path in root.glob("*.lnk"))
        self.assertEqual(left, ["no_arguments.lnk", "other.lnk", "other_tray.lnk"])

    def test_the_startup_shortcut_is_removed(self) -> None:
        source = UNINSTALLER.read_text(encoding="utf-8")
        for folder in ("Programs", "Desktop", "Startup"):
            with self.subTest(folder=folder):
                self.assertIn(
                    f"Remove-Shortcut (Join-Path ([Environment]::GetFolderPath('{folder}')) "
                    "'AirGrabber.lnk') $mainPy",
                    source,
                )


if __name__ == "__main__":
    unittest.main()
