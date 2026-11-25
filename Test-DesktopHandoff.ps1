# Deliver a stored probe or deployment task to Claude Desktop on Windows.
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [string]$PythonPath,
    [ValidatePattern('^DEP-(PROBE-)?[0-9A-F]{16}$')][string]$TaskId,
    [switch]$SendOnly
)

$ErrorActionPreference = 'Stop'
$python = if ($PythonPath) { $PythonPath } else { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
$probe = Join-Path $PSScriptRoot 'probe.py'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-Elements($root, $scope, $type) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::ControlTypeProperty, $type)
    $root.FindAll($scope, $condition)
}

function Test-ProbeText($element, [string]$expected) {
    $valuePattern = $null
    if ($element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$valuePattern)) {
        if ($valuePattern.Current.Value.Trim() -ceq $expected) { return $true }
    }
    $textPattern = $null
    if ($element.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern, [ref]$textPattern)) {
        return $textPattern.DocumentRange.GetText(-1).Trim() -ceq $expected
    }
    return $false
}

function Find-ProbeSendButton([string]$expected) {
    $descendants = [Windows.Automation.TreeScope]::Descendants
    $root = [Windows.Automation.AutomationElement]::RootElement
    $windows = Get-Elements $root ([Windows.Automation.TreeScope]::Children) ([Windows.Automation.ControlType]::Window)
    $matchesFound = @(
        foreach ($window in $windows) {
            $process = Get-Process -Id $window.Current.ProcessId -ErrorAction SilentlyContinue
            if ($null -eq $process -or $process.ProcessName -ine 'Claude') { continue }
            $windowPattern = $null
            if ($window.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$windowPattern)) {
                if ($windowPattern.Current.WindowVisualState -eq [Windows.Automation.WindowVisualState]::Minimized) {
                    $windowPattern.SetWindowVisualState([Windows.Automation.WindowVisualState]::Normal)
                }
            }
            $editors = @(Get-Elements $window $descendants ([Windows.Automation.ControlType]::Edit) |
                Where-Object { $_.Current.IsEnabled -and -not $_.Current.IsOffscreen -and (Test-ProbeText $_ $expected) })
            if ($editors.Count -ne 1) { continue }
            # Limit the search to the editor's nearby containers. Never send global key presses.
            $parent = $editors[0]
            for ($level = 0; $level -lt 7; $level++) {
                $parent = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
                if ($null -eq $parent -or $parent.Current.ControlType -in @(
                    [Windows.Automation.ControlType]::Document, [Windows.Automation.ControlType]::Window)) { break }
                $buttons = @(Get-Elements $parent $descendants ([Windows.Automation.ControlType]::Button) |
                    Where-Object { $_.Current.IsEnabled -and -not $_.Current.IsOffscreen -and
                        $_.Current.Name -match '^(Send|Send message|Send prompt|Submit|Submit prompt|Start session|\u53d1\u9001(\u6d88\u606f)?)\s*(\(Enter\))?$' })
                if ($buttons.Count -gt 1) { throw 'Ambiguous send button; no click attempted.' }
                if ($buttons.Count -eq 1) { $buttons[0]; break }
            }
        }
    )
    if ($matchesFound.Count -gt 1) { throw 'Multiple matching composers; no click attempted.' }
    if ($matchesFound.Count -eq 1) { return $matchesFound[0] }
}

function Get-ProbeState($id) {
    $command = 'status'
    if ($id -match '^DEP-[0-9A-F]{16}$') { $command = 'deployment-status' }
    $raw = & $python $probe $command $id
    if ($LASTEXITCODE -ne 0) { throw "Could not read probe $id" }
    $state = $raw | ConvertFrom-Json
    if ($state.task_id -cne $id) { throw 'Mismatched probe ID.' }
    return $state
}

function Set-ProbeStage([string]$stage) {
    & $python $probe stage $id $stage
    if ($LASTEXITCODE -ne 0) { throw "Could not record stage $stage for $id" }
}

foreach ($file in @($python, $probe)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing: $file" }
}
if (-not (Test-Path -LiteralPath 'Registry::HKEY_CLASSES_ROOT\claude')) { throw 'Claude URL handler not installed.' }
if ($CheckOnly) {
    Write-Output 'Preflight OK: probe, runtime, Windows UI Automation and Claude URL handler.'
    return
}

$id = $TaskId
if (-not $id) {
    $id = & $python $probe new
    if ($LASTEXITCODE -ne 0 -or $id -cnotmatch '^DEP-PROBE-[0-9A-F]{16}$') { throw 'Could not create probe.' }
}
$initialState = Get-ProbeState $id
if ($initialState.status -notin @('pending','queued')) { throw 'Expected a new pending task.' }
Write-Output "Task: $id"
Set-ProbeStage runtime_ready
$prompt = "Connection test. If mcp__cw-deploy-probe__acknowledge_probe is missing and WaitForMcpServers is available, call WaitForMcpServers with servers=['cw-deploy-probe'] and wait for its result. When ready, call mcp__cw-deploy-probe__acknowledge_probe exactly once with task_id $id and return its receipt. If the server fails, report its error. Use only these two tools."
if ($id -match '^DEP-[0-9A-F]{16}$') {
    $prompt = & $python $probe deployment-prompt $id
    if ($LASTEXITCODE -ne 0) { throw 'Could not read deployment prompt.' }
}
$uri = 'claude://code/new?q=' + [uri]::EscapeDataString($prompt)
Set-ProbeStage launching_desktop
$handler = (Get-Item 'Registry::HKEY_CLASSES_ROOT\claude\shell\open\command' -ErrorAction SilentlyContinue)
$handlerCommand = ''
if ($handler) { $handlerCommand = $handler.GetValue('') }
if ($handlerCommand -match '^"([^"]+)"' -and (Test-Path -LiteralPath $Matches[1])) {
    Start-Process -FilePath $uri
} else {
    # The Store can leave a per-user protocol command pointing at an old version.
    $package = Get-AppxPackage -Name Claude | Select-Object -First 1
    if (-not $package) { throw 'Claude Desktop package could not be located.' }
    $manifest = Get-AppxPackageManifest -Package $package.PackageFullName
    $application = $manifest.Package.Applications.Application | Where-Object {
        $_.SelectSingleNode(".//*[local-name()='Protocol' and @Name='claude']")
    } | Select-Object -First 1
    if (-not $application) { throw 'Claude Desktop protocol entry could not be located.' }
    Start-Process -FilePath (Join-Path $package.InstallLocation $application.Executable) -ArgumentList @('--', $uri)
}
Set-ProbeStage waiting_for_editor
$deadline = [datetime]::UtcNow.AddSeconds(45)
$invoked = $false
while ([datetime]::UtcNow -lt $deadline) {
    try { $button = Find-ProbeSendButton $prompt }
    catch [Windows.Automation.ElementNotAvailableException] { continue }
    if ($null -ne $button) {
        $invoke = $null
        if (-not $button.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
            throw "Send does not support UI Automation Invoke. Task: $id"
        }
        # A failure here has an unknown outcome. Do not click a second time.
        Set-ProbeStage invoking_send
        $sentAt = [DateTimeOffset]::UtcNow
        $invoke.Invoke()
        $invoked = $true
        Set-ProbeStage send_invoked
        if ($SendOnly -or $id -match '^DEP-[0-9A-F]{16}$') { return }
        Write-Output 'Send invoked; waiting for the MCP receipt.'
        break
    }
    Start-Sleep -Milliseconds 700
}
if (-not $invoked) { throw "Automatic send not verified: exact prompt and unique send button not found. Task: $id" }

$deadline = [datetime]::UtcNow.AddSeconds(120)
do {
    $state = Get-ProbeState $id
    if ($state.status -eq 'acknowledged' -and $state.receipt -cmatch '^ACK-[0-9A-F]{16}$' -and $state.acknowledged_at) {
        if ([DateTimeOffset]::Parse($state.acknowledged_at) -lt $sentAt) { throw "Receipt predates send. Task: $id" }
        Write-Output "PASS $id $($state.receipt) $($state.acknowledged_at)"
        return
    }
    Start-Sleep -Seconds 2
} while ([datetime]::UtcNow -lt $deadline)
throw "No MCP receipt within 120 seconds of send. Task: $id"
