#Requires -Version 5.1
<#
.SYNOPSIS
  Installs terminal-windows-node-agent on a WORKER Windows node (e.g. the
  Lenovo M910) -- run this ON THE WINDOWS NODE ITSELF, not on the
  controller (the Dell/Linux box). Mirrors deploy/install-node-agent.sh's
  own steps and printed final instructions -- see docs/multi-node.md for
  the full architecture and manual step-by-step if you'd rather not use
  this script.

.PARAMETER ControllerUrl
  The controller's terminal-mcp-http base URL, e.g. http://192.168.1.10:8766

.PARAMETER NodeId
  This node's own id (e.g. "m910") -- must match what you'll register on
  the controller's config.yaml nodes.remote[].node_id.

.PARAMETER Token
  This node's own shared secret. If omitted, a new one is generated (via
  .NET's own cryptographically-secure RNG) and printed once -- save it,
  it is never shown again by this script.

.PARAMETER RepoDir
  Where the terminal-mcp repo checkout lives (default: the parent of this
  script's own deploy\ directory -- i.e. run this FROM inside a checkout).

.PARAMETER Port
  This agent's own listen port (default 8790).

.PARAMETER Shell
  Default interactive shell for agent_type=shell sessions (default
  powershell.exe).

.PARAMETER HeartbeatIntervalSeconds
  Seconds between heartbeat pushes to the controller (default 20).

.PARAMETER SessionRoot
  Root directory a created session's working directory must resolve
  inside of (default: this node's own %USERPROFILE%). Only used when this
  script generates a config.yaml -- an existing one is never touched.

.PARAMETER NoScheduledTask
  Skip registering the Scheduled Task -- prints the command to run
  manually instead (for a non-Windows-Task-Scheduler environment, or to
  wire up a Windows Service via NSSM/WinSW yourself instead).

.EXAMPLE
  .\install-node-agent.ps1 -ControllerUrl http://192.168.1.10:8766 -NodeId m910

.NOTES
  Run from an elevated (Administrator) PowerShell prompt -- Register-
  ScheduledTask can fail with an access-denied error otherwise. If your
  execution policy blocks running local scripts, run once with:
    powershell.exe -ExecutionPolicy Bypass -File .\install-node-agent.ps1 ...
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ControllerUrl,
    [Parameter(Mandatory = $true)] [ValidatePattern('^[A-Za-z0-9_-]+$')] [string] $NodeId,
    [string] $Token,
    [string] $RepoDir = (Split-Path -Parent $PSScriptRoot),
    [int] $Port = 8790,
    [string] $BindHost = "127.0.0.1",
    [string] $ShellBinary = "powershell.exe",
    [double] $HeartbeatIntervalSeconds = 20,
    [switch] $NoScheduledTask,
    # The EXACT interpreter to build the venv from. Passed by
    # windows-setup.ps1, which resolves 3.12 deliberately. Left empty this
    # falls back to whatever `python` resolves to first, which is how a
    # node ended up with a Store-alias Python 3.14: pywinpty publishes no
    # 2.x wheel for cp314, so `pip install .[windows]` could not resolve
    # and the venv was left with nothing but pip in it.
    [string] $PythonExe,
    # Root a created session's working directory must resolve inside of,
    # written into the generated config.yaml. This node's own profile by
    # default -- never a drive root.
    [string] $SessionRoot = $env:USERPROFILE
)

$ErrorActionPreference = "Stop"

Write-Host "== Terminal MCP Windows node agent installer ==" -ForegroundColor Cyan
Write-Host "  node-id:    $NodeId"
Write-Host "  controller: $ControllerUrl"
Write-Host "  repo-dir:   $RepoDir"
Write-Host ""

# -- 1. Sanity: repo checkout -------------------------------------------------
if (-not (Test-Path (Join-Path $RepoDir "pyproject.toml"))) {
    Write-Error "No pyproject.toml found under $RepoDir -- pass -RepoDir pointing at a real terminal-mcp checkout (git clone it first if you haven't)."
    exit 1
}

# -- 2. Python -----------------------------------------------------------------
# An explicitly supplied interpreter wins over anything on PATH. The caller
# has already verified its version; re-deriving it here would defeat the
# point of passing it.
$pythonPath = $null
if ($PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Error "-PythonExe '$PythonExe' does not exist"
        exit 1
    }
    $pythonPath = $PythonExe
} else {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) { $pythonCmd = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $pythonCmd) {
        Write-Error "No 'python' or 'py' found on PATH -- install Python 3.11+ from python.org first (check 'Add to PATH' during setup)."
        exit 1
    }
    $pythonPath = $pythonCmd.Source
}
Write-Host "-> Using $pythonPath"

# -- 3. Venv + install -----------------------------------------------------------
$venvDir = Join-Path $RepoDir ".venv"
if (-not (Test-Path $venvDir)) {
    Write-Host "-> Creating .venv"
    & "$pythonPath" -m venv $venvDir
}
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"
Write-Host "-> Installing terminal-mcp (pip install -e .[windows])"
# pip writes to stderr for warnings as well as errors, and this script runs
# under $ErrorActionPreference = "Stop". On PowerShell 5.1 that turns the
# FIRST stderr line from a native command into a terminating
# NativeCommandError -- which killed the script here and made the fallback
# below unreachable, so a node whose [windows] extra could not resolve got
# no agent and no explanation. The preference is relaxed around the pip
# calls only, and the exit code is what decides instead.
function Invoke-Pip {
    param([Parameter(Mandatory)] [string[]] $PipArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $venvPip @PipArgs 2>&1 | ForEach-Object { Write-Host "   $_" }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

Invoke-Pip @('install', '--quiet', '--upgrade', 'pip') | Out-Null
$pipExit = Invoke-Pip @('install', '--quiet', '-e', "$RepoDir[windows]")
if ($pipExit -ne 0) {
    $warnMsg = "pip install -e .[windows] failed (pywinpty needs a C++ build toolchain on some Python versions) -- " `
        + "retrying without the [windows] extra; you will need to 'pip install pywinpty' yourself before this agent can actually spawn sessions."
    Write-Warning $warnMsg
    $pipExit = Invoke-Pip @('install', '--quiet', '-e', $RepoDir)
    if ($pipExit -ne 0) {
        Write-Error "pip install failed with and without the [windows] extra (exit $pipExit) -- see the lines above"
        exit 1
    }
}

# -- 4. Token -------------------------------------------------------------------
if (-not $Token) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $Token = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    Write-Host "-> Generated a new token (shown once below)"
}

# -- 5. config.yaml ---------------------------------------------------------------
$configPath = Join-Path $RepoDir "config.yaml"
if (-not (Test-Path $configPath)) {
    # deploy\node-agent-config.yaml, not config.example.yaml. The example is
    # the CONTROLLER's reference: session_lifecycle is commented out there,
    # so a node installed from it advertised session_transport, passed every
    # readiness check, and then refused the controller's first Create
    # Session with SESSION_LIFECYCLE_DISABLED. See that file's own header.
    $template = Join-Path $RepoDir "deploy\node-agent-config.yaml"
    if (Test-Path $template) {
        if (-not $SessionRoot) { throw "-SessionRoot is empty and %USERPROFILE% is not set -- refusing to write a config with no session root" }
        $root = $SessionRoot.TrimEnd('\')
        # YAML double-quoted scalar: a Windows path's own backslashes would
        # otherwise read as escapes.
        $yamlRoot = $root.Replace('\', '\\')
        (Get-Content $template -Raw).Replace('__SESSION_ROOT__', $yamlRoot) |
            Set-Content -Path $configPath -Encoding UTF8
        Write-Host "-> Wrote $configPath (sessions may be created under $root)"
    } else {
        Write-Host "-> No config.yaml and no node-agent template -- copying config.example.yaml as a starting point."
        Write-Host "   Review it (session_lifecycle.enabled + allowed_cwd_roots) before starting the agent."
        Copy-Item (Join-Path $RepoDir "config.example.yaml") $configPath
    }
}

# -- 6. Wrapper script (carries the token as an env var) + Scheduled Task -------
# State dir pinned to an EXPLICIT, absolute path under the repo checkout,
# never left to each store's own Path.home()-based default -- real bug
# found live bootstrapping this project's first actual Windows node:
# under a Scheduled Task (no interactively-loaded user profile), Path.
# home()/USERPROFILE resolution is NOT guaranteed to match what an
# interactive session sees, and sqlite3.connect() on the resulting wrong
# path fails outright ("unable to open database file") -- the agent
# process exits (LastTaskResult 0, a clean Python exit, easy to mistake
# for "it just isn't starting") the very first time it tries to open ANY
# store. Pinning every store's own env-var override here removes that
# ambiguity entirely, regardless of how Task Scheduler happens to resolve
# the profile in any given run.
$stateDir = Join-Path $RepoDir "state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$envVarName = "TERMINAL_MCP_NODE_TOKEN_" + ($NodeId.ToUpper() -replace '-', '_')
$wrapperPath = Join-Path $RepoDir "run-node-agent.ps1"
$wrapperContent = @"
# Generated by install-node-agent.ps1 -- do not commit this file (it is
# gitignored by default in this repo's own .gitignore pattern for
# run-*.ps1; verify that if you fork/customize this).
`$env:TERMINAL_MCP_NODE_TOKEN = "$Token"
`$env:TERMINAL_MCP_CONFIG = "$configPath"
`$env:TERMINAL_MCP_BINDINGS_DB = "$stateDir\bindings.db"
`$env:TERMINAL_MCP_AUDIT_DB = "$stateDir\audit.db"
`$env:TERMINAL_MCP_GRANTS_DB = "$stateDir\grants.db"
`$env:TERMINAL_MCP_LEASE_DB = "$stateDir\leases.db"
`$env:TERMINAL_MCP_KILLED_SESSIONS_DB = "$stateDir\killed-sessions.db"
& "$venvPython" -m terminal_mcp.windows_agent --node-id $NodeId --controller-url $ControllerUrl --host $BindHost --port $Port --shell $ShellBinary --heartbeat-interval-seconds $HeartbeatIntervalSeconds
"@
Set-Content -Path $wrapperPath -Value $wrapperContent -Encoding UTF8
Write-Host "-> Wrote $wrapperPath"

if (-not $NoScheduledTask) {
    $taskName = "TerminalMcpNodeAgent-$NodeId"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    # STOP the agent this task is already running before replacing it.
    # Register -Force rewrites the definition but leaves the running
    # instance alone, and that instance still holds the port -- so the
    # freshly started one died on bind and the node kept serving the OLD
    # version indefinitely. An upgrade that installs but never takes
    # effect is the worst of both: installed.json said 0.13.5-dev while
    # the live process was still running 0.13.3-dev.
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        # Bounded: the port has to be free before the replacement starts,
        # and waiting forever for a wedged process is its own failure.
        foreach ($wait in 1..20) {
            $held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if (-not $held) { break }
            Start-Sleep -Milliseconds 500
        }
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            Write-Warning "port $Port is still held after stopping '$taskName' -- the new agent may fail to bind"
        }
    }
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
            -Description "Terminal MCP node agent ($NodeId) -- auto-starts at logon, auto-restarts on failure" `
            -Force | Out-Null
        Write-Host "-> Registered Scheduled Task '$taskName' (Task Scheduler > Task Scheduler Library)"
        Start-ScheduledTask -TaskName $taskName
        Write-Host "-> Started. Check with: Get-ScheduledTask -TaskName '$taskName' | Get-ScheduledTaskInfo"
        Write-Host "   (Runs at logon; for a headless/always-on machine, also enable 'Run whether user is logged on or not'"
        Write-Host "    in Task Scheduler's GUI, or re-register with -RunLevel Highest and a saved credential.)"
    } catch {
        Write-Warning "Could not register the Scheduled Task automatically ($($_.Exception.Message)) -- run manually instead:"
        Write-Host "   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`""
    }
} else {
    Write-Host "-> -NoScheduledTask given: run manually with:"
    Write-Host "   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$wrapperPath`""
}

Write-Host ""
Write-Host "== Node agent installed. ONE MORE STEP -- on the CONTROLLER (Dell) ==" -ForegroundColor Cyan
Write-Host ""
Write-Host "1) Add this node to the controller's config.yaml (nodes.remote list):"
Write-Host ""
Write-Host "     nodes:"
Write-Host "       remote:"
Write-Host "         - node_id: $NodeId"
Write-Host "           display_name: `"$NodeId`""
Write-Host "           hostname: `"$env:COMPUTERNAME`""
Write-Host "           endpoint: `"http://<this-machine's-LAN-IP>:$Port`"  # verify reachable from the controller"
Write-Host "           token_env: $envVarName"
Write-Host ""
Write-Host "2) Export the SAME token this node generated as that environment variable"
Write-Host "   wherever the controller's terminal-mcp-http.service reads its own"
Write-Host "   environment from (never inline in the systemd unit itself):"
Write-Host ""
Write-Host "     $envVarName=$Token"
Write-Host ""
Write-Host "3) Safe-restart the controller's terminal-mcp-http.service -- verify existing"
Write-Host "   tmux sessions/session_created timestamps are unchanged before/after."
Write-Host ""
Write-Host "4) Verify with: terminal-mcp-doctor nodes   (on the controller)"
Write-Host "   -- $NodeId should show status=online within one heartbeat interval (~${HeartbeatIntervalSeconds}s)."
