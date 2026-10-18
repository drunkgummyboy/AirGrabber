"""install.ps1 and app/uninstall.ps1 in Windows PowerShell 5.1: both parse, the installer's
shortcuts carry AirGrabber's AppUserModelID, the installer sets up winget when it is missing (found /
registered / downloaded / given up, with Get-Command, Add-AppxPackage and Invoke-WebRequest stubbed),
its Prowlarr and failure windows say and do what they should, and the uninstaller removes this
install's shortcuts (the Startup one with its `--tray` included) and nobody else's.

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

    def test_the_installer_is_ascii(self) -> None:
        # Windows PowerShell 5.1 reads a script file without a BOM in the ANSI code page.
        self.assertTrue(INSTALLER.read_bytes().isascii())


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
