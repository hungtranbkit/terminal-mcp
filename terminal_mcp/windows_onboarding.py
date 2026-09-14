"""Generates `windows-setup.ps1` -- the ONE file a user downloads and runs
as Administrator to turn a Windows machine into a Terminal MCP node.

The script is generated rather than shipped static for exactly one
reason: it carries the enrollment code and the controller URL for THIS
enrollment. Everything else in it is a fixed, server-authored constant,
and the only value ever substituted into it goes through
`_ps_string()` -- a single-quoted PowerShell literal with embedded quotes
doubled, so a hostile node name cannot break out of the literal and
become code. There is no template loop, no user-supplied fragment, no
"extra commands" field: what runs on the machine is this file's own text
plus three short literals.

What the script does NOT contain, by construction and by test
(tests/test_windows_onboarding.py::test_script_carries_no_reusable_secret):

  * no bearer token          -- minted server-side, delivered once, in the
                                authenticated response to the enrollment
                                exchange the script performs at runtime
  * no Tailscale auth key    -- same
  * no gateway private key   -- the node generates its rescue keypair
                                locally and sends only the public half
  * no controller secret     -- there is none to send

The enrollment code IS in the file. That is the design: it is single-use,
expires in ~15 minutes, is revocable from the dashboard, and buys nothing
except one bootstrap payload for one machine.

Idempotency is a hard requirement, not a nice-to-have: the same script is
the installer, the `-Repair` tool, and the "run it again, I'm not sure it
worked" tool. Every step checks the current state first and only changes
what is actually wrong. `-Repair` is the same code path with the
enrollment exchange skipped (credentials already on disk).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# Bump when the script's own behavior changes. Reported by the node in
# its heartbeat labels, so the dashboard can say "this node was set up by
# an older installer" instead of leaving you to guess.
SCRIPT_VERSION = "1.0.0"

PROFILE_MINIMAL = "minimal"
PROFILE_DEVELOPER = "developer"
PROFILE_AI_CODING = "ai_coding"


# winget package ids. Every one of these is a real, first-party id on the
# public winget source -- deliberately no "install from a random URL"
# step anywhere in this file. A package that is already present is
# skipped by `winget install` itself (and by our own pre-check), which is
# what makes profile installation idempotent.
_PACKAGES: dict[str, tuple[tuple[str, str, str], ...]] = {
    # (winget id, friendly name, command that proves it is installed)
    PROFILE_MINIMAL: (),
    PROFILE_DEVELOPER: (
        ("Git.Git", "Git", "git"),
        ("Microsoft.PowerShell", "PowerShell 7", "pwsh"),
        ("Python.Python.3.12", "Python 3.12", "python"),
        ("OpenJS.NodeJS.LTS", "Node.js LTS", "node"),
        ("GitHub.cli", "GitHub CLI", "gh"),
    ),
    PROFILE_AI_CODING: (
        ("Git.Git", "Git", "git"),
        ("Microsoft.PowerShell", "PowerShell 7", "pwsh"),
        ("Python.Python.3.12", "Python 3.12", "python"),
        ("OpenJS.NodeJS.LTS", "Node.js LTS", "node"),
        ("GitHub.cli", "GitHub CLI", "gh"),
    ),
}

# npm-installed CLIs for the AI Coding profile. Installed only after
# Node.js is actually present; each one needs an interactive sign-in that
# no installer can do for the user, so the node reports `needs_signin`
# rather than Ready-with-a-lie. Terminal MCP never stores an AI token.
_NPM_TOOLS: dict[str, tuple[tuple[str, str, str], ...]] = {
    PROFILE_AI_CODING: (
        ("@anthropic-ai/claude-code", "Claude Code", "claude"),
        ("@openai/codex", "Codex CLI", "codex"),
        ("opencode-ai", "OpenCode", "opencode"),
    ),
}

PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_MINIMAL: {
        "id": PROFILE_MINIMAL,
        "label": "Minimal",
        "description": "OpenSSH + connectivity + node registration. Nothing else installed.",
        "packages": [],
        "npm_tools": [],
        "needs_signin": False,
    },
    PROFILE_DEVELOPER: {
        "id": PROFILE_DEVELOPER,
        "label": "Developer",
        "description": "Minimal, plus Git, PowerShell 7, Python, Node.js and the GitHub CLI (via winget).",
        "packages": [name for _id, name, _probe in _PACKAGES[PROFILE_DEVELOPER]],
        "npm_tools": [],
        "needs_signin": False,
    },
    PROFILE_AI_CODING: {
        "id": PROFILE_AI_CODING,
        "label": "AI Coding",
        "description": ("Developer, plus the Claude Code, Codex and OpenCode CLIs. Each needs an interactive "
                        "sign-in on the machine itself -- the node reports Needs sign-in until then, and no AI "
                        "credential is ever stored by Terminal MCP."),
        "packages": [name for _id, name, _probe in _PACKAGES[PROFILE_AI_CODING]],
        "npm_tools": [name for _id, name, _probe in _NPM_TOOLS[PROFILE_AI_CODING]],
        "needs_signin": True,
    },
}


def profile_summary(profile: str) -> dict[str, Any]:
    return dict(PROFILES.get(profile) or PROFILES[PROFILE_MINIMAL])


def list_profiles() -> list[dict[str, Any]]:
    return [dict(PROFILES[key]) for key in (PROFILE_MINIMAL, PROFILE_DEVELOPER, PROFILE_AI_CODING)]


_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{1,500}$")
_SAFE_CODE_RE = re.compile(r"^TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$")
_SAFE_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# The GENERIC (no-code) build served at /enroll/windows-setup.ps1. It is a
# real, valid code shape so the same renderer and the same validation
# apply, but it is all zeroes and the script refuses to run with it unless
# a real -EnrollmentCode was supplied on the command line.
GENERIC_CODE = "TMCP-00000-00000-00000"


def _ps_string(value: str) -> str:
    """A PowerShell SINGLE-quoted literal. Single-quoted strings in
    PowerShell interpolate nothing at all -- no `$var`, no backtick
    escape, no subexpression -- so the only character that can end the
    literal is `'`, and doubling it is the complete escape. This is the
    one and only way a value reaches the generated script."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_string_array(values) -> str:
    items = [_ps_string(v) for v in values]
    return "@(" + ", ".join(items) + ")" if items else "@()"


def render_setup_script(*, enrollment_code: str, controller_url: str, node_id: str,
                        display_name: str | None = None, profile: str = PROFILE_MINIMAL) -> str:
    """The full windows-setup.ps1 text. Raises ValueError on anything that
    would not be safe to embed -- a caller cannot opt out of that check."""
    if not _SAFE_CODE_RE.match(str(enrollment_code or "")):
        raise ValueError("enrollment_code is not a canonical enrollment code")
    if not _SAFE_URL_RE.match(str(controller_url or "")):
        raise ValueError("controller_url must be a plain http(s) URL")
    if not _SAFE_NODE_ID_RE.match(str(node_id or "")):
        raise ValueError("node_id is not a canonical node id")
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")

    winget_packages = _PACKAGES.get(profile, ())
    npm_tools = _NPM_TOOLS.get(profile, ())
    winget_literal = ",\n    ".join(
        f"@{{ Id = {_ps_string(pid)}; Name = {_ps_string(name)}; Probe = {_ps_string(probe)} }}"
        for pid, name, probe in winget_packages) or ""
    npm_literal = ",\n    ".join(
        f"@{{ Id = {_ps_string(pid)}; Name = {_ps_string(name)}; Probe = {_ps_string(probe)} }}"
        for pid, name, probe in npm_tools) or ""

    body = _SCRIPT_TEMPLATE
    body = body.replace("@@SCRIPT_VERSION@@", SCRIPT_VERSION)
    body = body.replace("@@ENROLLMENT_CODE@@", _ps_string(enrollment_code))
    body = body.replace("@@CONTROLLER_URL@@", _ps_string(controller_url.rstrip("/")))
    body = body.replace("@@NODE_ID@@", _ps_string(node_id))
    body = body.replace("@@DISPLAY_NAME@@", _ps_string(display_name or node_id))
    body = body.replace("@@PROFILE@@", _ps_string(profile))
    body = body.replace("@@WINGET_PACKAGES@@", ("\n    " + winget_literal + "\n") if winget_literal else "")
    body = body.replace("@@NPM_TOOLS@@", ("\n    " + npm_literal + "\n") if npm_literal else "")
    return body


# Short alias for the setup-script download, served alongside the long
# descriptive path. It exists for ONE reason: the Windows Run dialog
# (Win+R) truncates at ~259 characters, and the quick-install one-liner
# does not fit with the long path on a hostname of realistic length.
# See build_quick_install_command.
SETUP_SCRIPT_SHORT_PATH = "/w"
SETUP_SCRIPT_PATH = "/enroll/windows-setup.ps1"

# The Run dialog's own limit. Measured against it in build_quick_install_
# command so a too-long command is reported rather than silently handed to
# a user who would paste it and watch it get cut in half.
RUN_DIALOG_MAX_CHARS = 259


def build_quick_install_command(*, controller_url: str, enrollment_code: str) -> str:
    """The single line a user pastes into Win+R. Nothing else to type.

    What it does, and why each piece is the way it is:

      powershell -NoP -Command "..."
          No -ExecutionPolicy on the OUTER call on purpose: execution
          policy governs script FILES, never -Command, so adding it here
          would cost 11 characters of a 259-character budget and buy
          nothing.

      $f=$env:TEMP+'\tmcp.ps1'; iwr <url> -UseB -OutFile $f
          Download to a FILE, then run the file. Deliberately not
          `iex (irm ...)`: a file on disk can be read, kept and diffed
          after the fact, and an interrupted download fails at the
          download instead of executing half a script. -UseB
          (-UseBasicParsing) is required on Windows PowerShell 5.1, where
          Invoke-WebRequest otherwise needs Internet Explorer's engine to
          be initialised and fails on a fresh machine.

      saps powershell -Verb RunAs -Arg (...)
          -Verb RunAs is what raises the UAC prompt, so the user never
          opens an admin shell by hand. The elevated process gets
          -Ex Bypass, PROCESS scope only -- the machine policy is never
          touched.

      ('-NoP -Ex Bypass -File '+[char]34+$f+[char]34+' -EnrollmentCode ...')
          [char]34 is a double quote built without writing one. A literal
          quote here would terminate the outer -Command string, and the
          path MUST be quoted: %TEMP% for a user called "John Doe"
          contains a space, and Start-Process joins an argument ARRAY with
          spaces without quoting any element -- so the unquoted form
          breaks on exactly the machines most likely to be someone's
          personal laptop.

    The enrollment code travels as a command-line ARGUMENT, never as a URL
    query parameter: a code in a URL lands in the controller's access log,
    any proxy in between, and the user's own shell history. It is
    single-use and expires in minutes either way, but that is a reason to
    keep it short-lived, not a reason to spray it around.
    """
    if not _SAFE_CODE_RE.match(str(enrollment_code or "")):
        raise ValueError("enrollment_code is not a canonical enrollment code")
    base = str(controller_url or "").rstrip("/")
    if not _SAFE_URL_RE.match(base):
        raise ValueError("controller_url must be a plain http(s) URL")
    inner = (
        "$f=$env:TEMP+'\\tmcp.ps1';"
        f"iwr '{base}{SETUP_SCRIPT_SHORT_PATH}' -UseB -OutFile $f;"
        "saps powershell -Verb RunAs -Arg "
        "('-NoP -Ex Bypass -File '+[char]34+$f+[char]34+"
        f"' -EnrollmentCode {enrollment_code}')"
    )
    return f'powershell -NoP -Command "{inner}"'


def quick_install_fits_run_dialog(command: str) -> bool:
    return len(command) <= RUN_DIALOG_MAX_CHARS


def script_fingerprint(text: str) -> str:
    """sha256 of the generated script, printed next to the download so an
    operator can verify the file they are about to run as Administrator is
    the one the dashboard produced."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The template. `@@NAME@@` placeholders are the ONLY substitution points;
# everything else is fixed text. Written for Windows PowerShell 5.1, which
# every supported Windows ships with -- it must not require PowerShell 7,
# because installing PowerShell 7 is one of the things it does.
# ---------------------------------------------------------------------------

_SCRIPT_TEMPLATE = r"""#Requires -Version 5.1
<#
.SYNOPSIS
  Terminal MCP -- Windows node setup. Run this ONCE, as Administrator.

.DESCRIPTION
  Turns this machine into a Terminal MCP node:

    1. OpenSSH Server + Client, sshd set to Automatic, key-only auth
    2. Registers with the controller using the one-time enrollment code
       baked into this file (single-use, expires ~15 minutes after the
       dashboard generated it)
    3. Primary transport: Tailscale, unattended, if the controller has
       one configured -- otherwise the LAN address
    4. Rescue transport: a persistent reverse-SSH tunnel out to the
       controller's gateway, held open by a Scheduled Task that runs at
       system startup (before any user logs in) and reconnects forever
    5. A heartbeat Scheduled Task so the node shows Ready on the Dashboard

  Everything is idempotent. Running it twice changes nothing that is
  already correct.

.PARAMETER Repair
  Re-check and fix everything, WITHOUT consuming an enrollment code --
  uses the credentials already on disk. This is the "it stopped working"
  button.

.PARAMETER Uninstall
  Remove everything Terminal MCP installed: both Scheduled Tasks, the
  rescue keypair, the controller's authorized_key, the state directory.
  Does NOT uninstall OpenSSH, Tailscale, Git, Python, Node or any AI CLI
  -- those are the machine's own software, not ours to remove.

.PARAMETER SkipProfilePackages
  Do the connectivity/registration work only; skip the winget/npm
  installs. Useful on a metered connection.

.NOTES
  Exit codes:  0 = everything green
               1 = a required step failed (see the checklist)
               2 = up and registered, but one or more optional steps are
                   degraded (e.g. no rescue gateway configured)
               3 = not elevated and could not self-elevate
#>
[CmdletBinding()]
param(
    [switch] $Repair,
    [switch] $Uninstall,
    [switch] $SkipProfilePackages,
    [string] $EnrollmentCode,
    [string] $ControllerUrl
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# -- values baked in by the dashboard when this file was generated ----------
$ScriptVersion   = '@@SCRIPT_VERSION@@'
$BakedCode       = @@ENROLLMENT_CODE@@
$BakedController = @@CONTROLLER_URL@@
$NodeId          = @@NODE_ID@@
$DisplayName     = @@DISPLAY_NAME@@
$ProfileName     = @@PROFILE@@

$WingetPackages = @(@@WINGET_PACKAGES@@)
$NpmTools = @(@@NPM_TOOLS@@)

if (-not $EnrollmentCode) { $EnrollmentCode = $BakedCode }
if (-not $ControllerUrl)  { $ControllerUrl  = $BakedController }

# The generic build (downloaded from /enroll/windows-setup.ps1) has no real
# code baked in. Refuse loudly rather than fail four steps later with an
# opaque 401 from the controller.
if ($EnrollmentCode -eq 'TMCP-00000-00000-00000' -and -not ($Repair -or $Uninstall)) {
    Write-Host 'This is the generic setup script -- it needs an enrollment code.' -ForegroundColor Red
    Write-Host 'Open the Dashboard -> Nodes -> + Add Node -> Windows, generate a code, then run:' -ForegroundColor Yellow
    Write-Host '  .\windows-setup.ps1 -EnrollmentCode TMCP-XXXXX-XXXXX-XXXXX' -ForegroundColor Yellow
    Write-Host '(or just use the Download button, which bakes the code in for you)' -ForegroundColor Yellow
    exit 1
}

$StateDir     = Join-Path $env:ProgramData 'TerminalMCP'
$NodeConfig   = Join-Path $StateDir 'node.json'
$TokenFile    = Join-Path $StateDir 'node.token'
$RescueDir    = Join-Path $StateDir 'rescue'
$RescueKey    = Join-Path $RescueDir 'id_ed25519'
$RescueKnown  = Join-Path $RescueDir 'known_hosts'
$RescueRunner = Join-Path $StateDir 'rescue-tunnel.ps1'
$BeatRunner   = Join-Path $StateDir 'heartbeat.ps1'
$LogDir       = Join-Path $StateDir 'logs'
$TaskRescue   = "TerminalMCP-Rescue-$NodeId"
$TaskBeat     = "TerminalMCP-Heartbeat-$NodeId"

# -- checklist --------------------------------------------------------------
# Every step appends exactly one row here, and the script's own exit code
# is derived from these rows -- there is no separate "did it work"
# bookkeeping that could disagree with what gets printed.
$script:Checklist = New-Object System.Collections.ArrayList
$script:TotalSw = [System.Diagnostics.Stopwatch]::StartNew()
$script:StageNo = 0
$script:StageTotal = 11
$script:StageSw = $null

# -- stage banner + elapsed ---------------------------------------------
# The console is the only thing the user can see while this runs, and the
# slowest step (Add-WindowsCapability for OpenSSH) can sit for minutes
# with no output of its own. Silence there is indistinguishable from a
# hang, so every stage announces itself with a wall-clock time and closes
# with its own elapsed seconds plus the running total.
function Start-Stage {
    param([Parameter(Mandatory)] [string] $Name)
    $script:StageNo++
    $script:StageSw = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $script:StageNo, $script:StageTotal, $Name) -ForegroundColor Cyan -NoNewline
    Write-Host ("   {0}" -f (Get-Date -Format 'HH:mm:ss')) -ForegroundColor DarkGray
}
function End-Stage {
    param([string] $State = 'OK')
    $each = if ($script:StageSw) { [int]$script:StageSw.Elapsed.TotalSeconds } else { 0 }
    $all = [int]$script:TotalSw.Elapsed.TotalSeconds
    $color = switch ($State) { 'OK' { 'DarkGray' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'DarkGray' } }
    Write-Host ("      -> {0} ({1}s, tong cong {2}s)" -f $State, $each, $all) -ForegroundColor $color
}

# -- progress reported to the Dashboard ---------------------------------
# Best effort, always. A controller that cannot be reached yet (the node
# may still be joining Tailscale) must never fail an install -- the whole
# point of this call is to make a slow install legible, not to gate it.
# The enrollment code authenticates it; it is never written to the log.
function Report-Stage {
    param([Parameter(Mandatory)] [string] $Stage)
    if (-not $ControllerUrl -or -not $EnrollmentCode) { return }
    try {
        $payload = @{ code = $EnrollmentCode; stage = $Stage
                      elapsed_seconds = [int]$script:TotalSw.Elapsed.TotalSeconds } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "$ControllerUrl/dashboard/api/enroll/progress" `
            -ContentType 'application/json' -Body $payload -TimeoutSec 8 -UseBasicParsing | Out-Null
    } catch { }
}

# -- run something slow without going silent ----------------------------
function Invoke-Tracked {
    param(
        [Parameter(Mandatory)] [scriptblock] $Work,
        [Parameter(Mandatory)] [string] $What,
        [int] $SlowAfterSeconds = 120,
        [int] $TimeoutSeconds = 900,
        [string] $Stage = ''
    )
    # A background job, polled -- rather than calling $Work inline, which
    # would block with no way to print anything until it returned.
    $job = Start-Job -ScriptBlock $Work
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $lastBeat = 0
    $warned = $false
    try {
        while ($job.State -eq 'Running') {
            Start-Sleep -Seconds 2
            $secs = [int]$sw.Elapsed.TotalSeconds
            # Stream whatever the job has produced so far, instead of
            # holding it all until the job ends.
            foreach ($line in @(Receive-Job -Job $job -ErrorAction SilentlyContinue)) {
                $text = [string]$line
                if ($text.Trim()) { Write-Host ("      | " + $text.Trim()) -ForegroundColor DarkGray }
            }
            if (($secs - $lastBeat) -ge 12) {
                $lastBeat = $secs
                if ($secs -ge $SlowAfterSeconds) {
                    if (-not $warned) {
                        $warned = $true
                        Write-Host ("      ! Dang cho lau hon binh thuong ({0}s). {1} van dang chay --" -f $secs, $What) -ForegroundColor Yellow
                        Write-Host  "        dung tat cua so nay. Windows Update/winget co the mat vai phut." -ForegroundColor Yellow
                    } else {
                        Write-Host ("      ! Van dang chay... {0}s (qua nguong {1}s)" -f $secs, $SlowAfterSeconds) -ForegroundColor Yellow
                    }
                } else {
                    Write-Host ("      ... Van dang chay... {0}s" -f $secs) -ForegroundColor DarkGray
                }
                if ($Stage) { Report-Stage $Stage }
            }
            if ($secs -ge $TimeoutSeconds) {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
                throw ("{0}: qua {1}s ma chua xong -- da dung buoc nay." -f $What, $TimeoutSeconds)
            }
        }
        foreach ($line in @(Receive-Job -Job $job -ErrorAction SilentlyContinue)) {
            $text = [string]$line
            if ($text.Trim()) { Write-Host ("      | " + $text.Trim()) -ForegroundColor DarkGray }
        }
        if ($job.State -eq 'Failed') {
            $reason = ($job.ChildJobs | ForEach-Object { $_.JobStateInfo.Reason.Message }) -join '; '
            throw ("{0} that bai: {1}" -f $What, $reason)
        }
    } finally {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
}
function Add-Step {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [ValidateSet('OK', 'WARN', 'FAIL', 'SKIP')] [string] $State,
        [string] $Detail = '',
        [string] $Fix = ''
    )
    [void]$script:Checklist.Add([pscustomobject]@{ Name = $Name; State = $State; Detail = $Detail; Fix = $Fix })
    $color = switch ($State) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'DarkGray' } }
    $line = '  [{0,-4}] {1}' -f $State, $Name
    if ($Detail) { $line += "  --  $Detail" }
    Write-Host $line -ForegroundColor $color
}
function Write-Section { param([string] $Text) Write-Host "`n== $Text ==" -ForegroundColor Cyan }

# Secrets never reach the transcript. Anything printed goes through this.
function Protect-Secret {
    param([string] $Text)
    if (-not $Text) { return '' }
    if ($Text.Length -le 6) { return '***' }
    return $Text.Substring(0, 4) + '...' + '*' * 6
}

# -- elevation --------------------------------------------------------------
function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated)) {
    Write-Host 'This setup must run as Administrator. Requesting elevation...' -ForegroundColor Yellow
    $self = $MyInvocation.MyCommand.Path
    if (-not $self) {
        Write-Host 'Cannot self-elevate: run this file from disk, not from a pipe.' -ForegroundColor Red
        Write-Host 'Right-click the .ps1 -> Run with PowerShell (as Administrator), or:' -ForegroundColor Yellow
        Write-Host '  Start-Process powershell -Verb RunAs -ArgumentList ''-ExecutionPolicy'',''Bypass'',''-File'',''<path to this file>'''
        exit 3
    }
    # Forward the switches we were given; the code stays inside the file.
    $fwd = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $self)
    if ($Repair) { $fwd += '-Repair' }
    if ($Uninstall) { $fwd += '-Uninstall' }
    if ($SkipProfilePackages) { $fwd += '-SkipProfilePackages' }
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $fwd -PassThru -Wait
        exit $proc.ExitCode
    } catch {
        Write-Host "Elevation was declined or failed: $($_.Exception.Message)" -ForegroundColor Red
        exit 3
    }
}

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host " Terminal MCP -- Windows node setup  (v$ScriptVersion)" -ForegroundColor Cyan
Write-Host "   node:       $NodeId"
Write-Host "   profile:    $ProfileName"
Write-Host "   controller: $ControllerUrl"
Write-Host '=====================================================' -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $StateDir, $RescueDir, $LogDir | Out-Null

# ===========================================================================
#  UNINSTALL
# ===========================================================================
if ($Uninstall) {
    Write-Section 'Removing Terminal MCP node configuration'
    if (Test-Path $NodeConfig) {
        try {
            $saved = Get-Content $NodeConfig -Raw | ConvertFrom-Json
            if ($saved.node_id) {
                $NodeId = [string]$saved.node_id
                $TaskRescue = "TerminalMCP-Rescue-$NodeId"
                $TaskBeat = "TerminalMCP-Heartbeat-$NodeId"
            }
        } catch { }
    }
    foreach ($task in @($TaskRescue, $TaskBeat)) {
        try {
            if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $task -Confirm:$false
                Add-Step "Scheduled Task $task" 'OK' 'removed'
            } else {
                Add-Step "Scheduled Task $task" 'SKIP' 'not present'
            }
        } catch { Add-Step "Scheduled Task $task" 'WARN' $_.Exception.Message }
    }
    # Tell the controller to revoke this node, best effort -- a node being
    # decommissioned is often already off the network.
    try {
        if ((Test-Path $TokenFile) -and (Test-Path $NodeConfig)) {
            $cfg = Get-Content $NodeConfig -Raw | ConvertFrom-Json
            $tok = (Get-Content $TokenFile -Raw).Trim()
            $headers = @{ Authorization = "Bearer $tok" }
            Invoke-RestMethod -Method Post -Uri "$($cfg.controller_url)/dashboard/api/nodes/$NodeId/deregister" `
                -Headers $headers -TimeoutSec 15 -UseBasicParsing | Out-Null
            Add-Step 'Controller deregistration' 'OK' 'node revoked on the controller'
        } else {
            Add-Step 'Controller deregistration' 'SKIP' 'no local credentials'
        }
    } catch {
        Add-Step 'Controller deregistration' 'WARN' 'could not reach the controller -- remove the node from the Dashboard'
    }
    # Drop OUR authorized_key only -- never the whole file, which may hold
    # keys this machine's owner put there.
    try {
        $adminKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        if ((Test-Path $adminKeys) -and (Test-Path $NodeConfig)) {
            $cfg = Get-Content $NodeConfig -Raw | ConvertFrom-Json
            if ($cfg.PSObject.Properties.Name -contains 'controller_key' -and $cfg.controller_key) {
                $kept = Get-Content $adminKeys | Where-Object { $_.Trim() -ne $cfg.controller_key.Trim() }
                Set-Content -Path $adminKeys -Value $kept -Encoding ascii
                Add-Step 'Controller authorized_key' 'OK' 'removed (other keys left untouched)'
            }
        }
    } catch { Add-Step 'Controller authorized_key' 'WARN' $_.Exception.Message }
    try {
        Get-NetFirewallRule -DisplayName 'Terminal MCP - OpenSSH inbound' -ErrorAction SilentlyContinue |
            Remove-NetFirewallRule -ErrorAction SilentlyContinue
        Add-Step 'Firewall rule' 'OK' 'removed'
    } catch { Add-Step 'Firewall rule' 'SKIP' 'not present' }
    try {
        Remove-Item -Recurse -Force $StateDir -ErrorAction Stop
        Add-Step 'State directory' 'OK' $StateDir
    } catch { Add-Step 'State directory' 'WARN' $_.Exception.Message }
    Write-Host ''
    Write-Host 'Terminal MCP node configuration removed. OpenSSH, Tailscale and any developer' -ForegroundColor Green
    Write-Host 'tooling were left installed -- they are this machine''s software, not ours.' -ForegroundColor Green
    exit 0
}

# ===========================================================================
#  1. OpenSSH Server + Client
# ===========================================================================
Start-Stage 'Kiem tra quyen Administrator va phien ban Windows'
Report-Stage 'starting'
try {
    $os = Get-CimInstance Win32_OperatingSystem
    Add-Step 'Administrator' 'OK' 'da elevated'
    Add-Step 'Windows' 'OK' ("{0} (build {1})" -f $os.Caption, $os.BuildNumber)
} catch { Add-Step 'Windows' 'WARN' $_.Exception.Message }
End-Stage

Start-Stage 'Cai OpenSSH Server (buoc nay co the mat vai phut)'
Report-Stage 'installing_openssh'
$sshdReady = $false
try {
    $caps = Get-WindowsCapability -Online -Name 'OpenSSH*' -ErrorAction Stop
    foreach ($want in @('OpenSSH.Server', 'OpenSSH.Client')) {
        $cap = $caps | Where-Object { $_.Name -like "$want*" } | Select-Object -First 1
        if ($null -eq $cap) {
            Add-Step "$want" 'WARN' 'capability not offered by this Windows build'
            continue
        }
        if ($cap.State -ne 'Installed') {
            Write-Host "      dang cai $want ..." -ForegroundColor DarkGray
            # The single slowest thing this script does. Tracked so the
            # console keeps talking while DISM works.
            $capName = $cap.Name
            Invoke-Tracked -What "Cai $want" -Stage 'installing_openssh' `
                -SlowAfterSeconds 150 -TimeoutSeconds 1800 `
                -Work ([scriptblock]::Create("Add-WindowsCapability -Online -Name '$capName' | Out-Null"))
            Add-Step "$want" 'OK' 'installed'
        } else {
            Add-Step "$want" 'OK' 'already installed'
        }
    }
    End-Stage
    Start-Stage 'Khoi dong dich vu sshd'
    Set-Service -Name sshd -StartupType Automatic
    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
    if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }
    Add-Step 'sshd service' 'OK' 'Automatic + running'
    $sshdReady = $true
    End-Stage
} catch {
    Add-Step 'OpenSSH Server' 'FAIL' $_.Exception.Message `
        'Install manually: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
    End-Stage 'FAIL'
}

# Key-only auth, written as a drop-in we own rather than by editing the
# machine's sshd_config in place -- so an operator's own settings survive,
# and -Uninstall knows exactly what to take back.
if ($sshdReady) {
    try {
        $sshdConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
        $dropInDir  = Join-Path $env:ProgramData 'ssh\sshd_config.d'
        $dropIn     = Join-Path $dropInDir '10-terminal-mcp.conf'
        New-Item -ItemType Directory -Force -Path $dropInDir | Out-Null
        $dropInBody = @(
            '# Managed by Terminal MCP. Edits here are overwritten on repair.',
            'PubkeyAuthentication yes',
            'PasswordAuthentication no',
            'PermitEmptyPasswords no',
            'KbdInteractiveAuthentication no'
        ) -join "`r`n"
        Set-Content -Path $dropIn -Value $dropInBody -Encoding ascii
        # Windows' bundled sshd_config does not always Include the drop-in
        # directory. Add the Include line at the TOP if it is missing --
        # sshd takes the first value it sees for most keywords, so the
        # include has to come first to actually win.
        $existing = Get-Content $sshdConfig -ErrorAction SilentlyContinue
        if ($null -eq $existing) { $existing = @() }
        if (-not ($existing | Where-Object { $_ -match '^\s*Include\s+.*sshd_config\.d' })) {
            $updated = @("Include __PROGRAMDATA__/ssh/sshd_config.d/*.conf") + $existing
            Set-Content -Path $sshdConfig -Value $updated -Encoding ascii
        }
        Restart-Service sshd
        Add-Step 'sshd key-only auth' 'OK' 'PasswordAuthentication no (drop-in 10-terminal-mcp.conf)'
    } catch {
        Add-Step 'sshd key-only auth' 'WARN' $_.Exception.Message `
            'Set PasswordAuthentication no in C:\ProgramData\ssh\sshd_config and restart sshd'
    }
}

# ===========================================================================
#  2. Rescue keypair (generated BEFORE enrolling -- the public half goes
#     in the enrollment request so the controller can allocate a port and
#     stage the gateway authorized_keys line in one round trip)
# ===========================================================================
Start-Stage 'Cau hinh SSH (khoa rescue + chinh sach dang nhap)'
Report-Stage 'configuring_ssh'
$rescuePublicKey = ''
try {
    if (-not (Test-Path "$RescueKey.pub")) {
        & ssh-keygen.exe -t ed25519 -N '""' -C "terminal-mcp-rescue-$NodeId" -f $RescueKey -q
    }
    if (Test-Path "$RescueKey.pub") {
        $rescuePublicKey = (Get-Content "$RescueKey.pub" -Raw).Trim()
        # Private key readable by SYSTEM and Administrators only. OpenSSH on
        # Windows refuses a key with looser ACLs, so this is correctness as
        # much as hygiene.
        icacls $RescueKey /inheritance:r /grant 'SYSTEM:(R)' /grant 'Administrators:(R)' | Out-Null
        Add-Step 'Rescue keypair' 'OK' 'ed25519, private key locked to SYSTEM + Administrators'
    } else {
        Add-Step 'Rescue keypair' 'WARN' 'ssh-keygen produced no key -- rescue tunnel will be skipped'
    }
} catch {
    Add-Step 'Rescue keypair' 'WARN' $_.Exception.Message
}

# ===========================================================================
#  3. Enrollment exchange  (or reuse existing credentials under -Repair)
# ===========================================================================
End-Stage
Start-Stage 'Dang ky node voi controller'
Report-Stage 'registering'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11

function Get-LocalAddresses {
    $tailscale = $null
    $lan = $null
    try {
        $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' }
        foreach ($a in $addrs) {
            $ip = $a.IPAddress
            $octet2 = [int]($ip.Split('.')[1])
            if ($ip.StartsWith('100.') -and $octet2 -ge 64 -and $octet2 -le 127) {
                if (-not $tailscale) { $tailscale = $ip }
            } elseif (-not $lan) {
                $lan = $ip
            }
        }
    } catch { }
    return @{ tailscale_ip = $tailscale; lan_ip = $lan }
}

$bootstrap = $null
if ($Repair) {
    if (Test-Path $NodeConfig) {
        $bootstrap = Get-Content $NodeConfig -Raw | ConvertFrom-Json
        if ($bootstrap.node_id) {
            $NodeId = [string]$bootstrap.node_id
            $TaskRescue = "TerminalMCP-Rescue-$NodeId"
            $TaskBeat = "TerminalMCP-Heartbeat-$NodeId"
        }
        if ($bootstrap.controller_url) { $ControllerUrl = [string]$bootstrap.controller_url }
        Add-Step 'Enrollment' 'SKIP' '-Repair: reusing the credentials already on disk'
    } else {
        Add-Step 'Enrollment' 'FAIL' 'no saved configuration to repair' `
            'Generate a fresh setup script from the Dashboard and run it without -Repair'
    }
} else {
    $addresses = Get-LocalAddresses
    $body = @{
        code             = $EnrollmentCode
        hostname         = $env:COMPUTERNAME
        platform         = 'windows'
        os_version       = [string](Get-CimInstance Win32_OperatingSystem).Caption
        script_version   = $ScriptVersion
        addresses        = $addresses
        rescue_public_key = $rescuePublicKey
    } | ConvertTo-Json -Depth 5
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$ControllerUrl/dashboard/api/enroll/consume" `
            -ContentType 'application/json' -Body $body -TimeoutSec 45 -UseBasicParsing
        $bootstrap = $response
        # Persist the bootstrap payload WITHOUT the secret, and the secret
        # in its own file with a tight ACL. Two files, one reason: the
        # config is fine to read for diagnostics, the token never is.
        $tokenValue = [string]$bootstrap.node_token
        $safe = $bootstrap | Select-Object * -ExcludeProperty node_token
        # The Tailscale auth key is used once, below, and never written to disk.
        if ($safe.PSObject.Properties.Name -contains 'tailscale') {
            $safe.tailscale = $safe.tailscale | Select-Object * -ExcludeProperty auth_key
        }
        $safe | Add-Member -NotePropertyName controller_url -NotePropertyValue $ControllerUrl -Force
        $safe | Add-Member -NotePropertyName controller_key `
            -NotePropertyValue ([string]$bootstrap.ssh.authorized_key) -Force
        $safe | ConvertTo-Json -Depth 8 | Set-Content -Path $NodeConfig -Encoding utf8
        Set-Content -Path $TokenFile -Value $tokenValue -Encoding ascii -NoNewline
        icacls $TokenFile /inheritance:r /grant 'SYSTEM:(R)' /grant 'Administrators:(R)' | Out-Null
        # The CONTROLLER is authoritative for the node id -- the generic
        # build has none baked in, and even the generated one could have
        # been superseded. Adopt what came back, and re-derive every name
        # derived from it, before anything else uses it.
        if ($bootstrap.node_id) {
            $NodeId = [string]$bootstrap.node_id
            $TaskRescue = "TerminalMCP-Rescue-$NodeId"
            $TaskBeat = "TerminalMCP-Heartbeat-$NodeId"
        }
        Add-Step 'Enrollment' 'OK' "registered as node '$NodeId'"
    } catch {
        $detail = $_.Exception.Message
        try {
            $stream = $_.Exception.Response.GetResponseStream()
            $detail = (New-Object IO.StreamReader($stream)).ReadToEnd()
        } catch { }
        Add-Step 'Enrollment' 'FAIL' $detail `
            'The code is single-use and expires in ~15 minutes -- generate a new setup script from the Dashboard'
    }
}

# ===========================================================================
#  4. Controller SSH key + firewall
# ===========================================================================
End-Stage
Start-Stage 'Cau hinh authorized_keys va firewall'
if ($bootstrap -and $bootstrap.ssh -and $bootstrap.ssh.authorized_key) {
    try {
        $adminKeys = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        $key = ([string]$bootstrap.ssh.authorized_key).Trim()
        $lines = @()
        if (Test-Path $adminKeys) { $lines = @(Get-Content $adminKeys) }
        if ($lines -notcontains $key) { $lines += $key }
        Set-Content -Path $adminKeys -Value $lines -Encoding ascii
        # The exact ACL Windows' sshd requires on this file: SYSTEM and
        # Administrators only, inheritance off. Anything else and sshd
        # silently refuses every key in it.
        icacls $adminKeys /inheritance:r /grant 'SYSTEM:(F)' /grant 'BUILTIN\Administrators:(F)' | Out-Null
        Add-Step 'Controller authorized_key' 'OK' 'installed in administrators_authorized_keys'
    } catch {
        Add-Step 'Controller authorized_key' 'FAIL' $_.Exception.Message
    }
} else {
    Add-Step 'Controller authorized_key' 'WARN' 'the controller has no SSH public key configured' `
        'Set nodes.onboarding.controller_ssh_public_key on the controller, then re-run with -Repair'
}

try {
    $cidrs = @()
    if ($bootstrap -and $bootstrap.ssh -and $bootstrap.ssh.firewall_cidrs) {
        $cidrs = @($bootstrap.ssh.firewall_cidrs)
    }
    Get-NetFirewallRule -DisplayName 'Terminal MCP - OpenSSH inbound' -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    if ($cidrs.Count -gt 0 -and ($cidrs -notcontains 'any')) {
        New-NetFirewallRule -DisplayName 'Terminal MCP - OpenSSH inbound' -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort 22 -RemoteAddress $cidrs -Profile Any | Out-Null
        Add-Step 'Firewall (inbound 22)' 'OK' ('scoped to ' + ($cidrs -join ', '))
    } elseif ($cidrs -contains 'any') {
        New-NetFirewallRule -DisplayName 'Terminal MCP - OpenSSH inbound' -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort 22 -Profile Any | Out-Null
        Add-Step 'Firewall (inbound 22)' 'WARN' 'open to ANY source -- the controller config asked for it'
    } else {
        Add-Step 'Firewall (inbound 22)' 'SKIP' 'no source range configured; no inbound rule added'
    }
} catch {
    Add-Step 'Firewall (inbound 22)' 'WARN' $_.Exception.Message
}

# ===========================================================================
#  5. Primary transport -- Tailscale
# ===========================================================================
End-Stage
Start-Stage 'Tailscale / duong ket noi chinh'
$tailscaleWanted = $false
if ($bootstrap -and $bootstrap.tailscale) { $tailscaleWanted = [bool]$bootstrap.tailscale.enabled }

if (-not $tailscaleWanted) {
    $why = 'not requested'
    if ($bootstrap -and $bootstrap.tailscale -and $bootstrap.tailscale.reason) { $why = [string]$bootstrap.tailscale.reason }
    Add-Step 'Tailscale (primary)' 'SKIP' $why
} else {
    $tsExe = $null
    foreach ($candidate in @("$env:ProgramFiles\Tailscale\tailscale.exe",
                             "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {
        if (Test-Path $candidate) { $tsExe = $candidate; break }
    }
    if (-not $tsExe) {
        try {
            Write-Host '  installing Tailscale via winget ...'
            & winget install --id Tailscale.Tailscale --silent --accept-package-agreements `
                --accept-source-agreements --disable-interactivity 2>&1 | Out-Null
            foreach ($candidate in @("$env:ProgramFiles\Tailscale\tailscale.exe",
                                     "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {
                if (Test-Path $candidate) { $tsExe = $candidate; break }
            }
        } catch { }
    }
    if (-not $tsExe) {
        Add-Step 'Tailscale (primary)' 'WARN' 'not installed and winget could not install it' `
            'Install Tailscale from https://tailscale.com/download/windows, then re-run with -Repair'
    } else {
        try {
            Set-Service -Name Tailscale -StartupType Automatic -ErrorAction SilentlyContinue
            if ((Get-Service Tailscale -ErrorAction SilentlyContinue).Status -ne 'Running') {
                Start-Service Tailscale -ErrorAction SilentlyContinue
            }
            $status = & $tsExe status --json 2>$null | ConvertFrom-Json
            $loggedIn = $false
            if ($status -and $status.Self -and $status.Self.TailscaleIPs) { $loggedIn = $true }
            if (-not $loggedIn) {
                $authKey = $null
                if ($bootstrap.tailscale.PSObject.Properties.Name -contains 'auth_key') {
                    $authKey = [string]$bootstrap.tailscale.auth_key
                }
                $upArgs = @('up', '--unattended')
                if ($bootstrap.tailscale.login_server) { $upArgs += "--login-server=$($bootstrap.tailscale.login_server)" }
                if ($bootstrap.tailscale.tags -and @($bootstrap.tailscale.tags).Count -gt 0) {
                    $upArgs += "--advertise-tags=$((@($bootstrap.tailscale.tags)) -join ',')"
                }
                if ($authKey) {
                    # The key is passed as one argv element and never echoed.
                    & $tsExe @upArgs "--authkey=$authKey" 2>&1 | Out-Null
                    $status = & $tsExe status --json 2>$null | ConvertFrom-Json
                    if ($status -and $status.Self -and $status.Self.TailscaleIPs) {
                        Add-Step 'Tailscale (primary)' 'OK' 'joined, unattended'
                    } else {
                        Add-Step 'Tailscale (primary)' 'WARN' 'the auth key did not complete a login' `
                            'Run: tailscale up --unattended   (interactive browser login)'
                    }
                } else {
                    Add-Step 'Tailscale (primary)' 'WARN' 'no auth key from the controller -- sign in once by hand' `
                        'Run: tailscale up --unattended   (a browser opens; after that it survives reboots)'
                }
            } else {
                & $tsExe up --unattended 2>&1 | Out-Null   # idempotent: only sets the unattended flag
                Add-Step 'Tailscale (primary)' 'OK' 'already signed in; unattended confirmed'
            }
        } catch {
            Add-Step 'Tailscale (primary)' 'WARN' $_.Exception.Message
        }
    }
}

# ===========================================================================
#  6. Rescue transport -- persistent reverse SSH
# ===========================================================================
End-Stage
Start-Stage 'Duong rescue (reverse SSH)'
$rescueConfigured = $false
if ($bootstrap -and $bootstrap.rescue) { $rescueConfigured = [bool]$bootstrap.rescue.configured }

if (-not $rescueConfigured) {
    $why = 'no rescue gateway configured on the controller'
    if ($bootstrap -and $bootstrap.rescue -and $bootstrap.rescue.reason) { $why = [string]$bootstrap.rescue.reason }
    Add-Step 'Rescue tunnel' 'SKIP' $why `
        'Optional. Configure nodes.onboarding.rescue on the controller and re-run with -Repair'
    # A stale task from an earlier run must not keep dialling a gateway
    # that is no longer configured.
    if (Get-ScheduledTask -TaskName $TaskRescue -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskRescue -Confirm:$false
    }
} else {
    try {
        Set-Content -Path $RescueKnown -Value ([string]$bootstrap.rescue.host_key) -Encoding ascii
        $sshArgs = @(
            '-N', '-T',
            '-i', $RescueKey,
            '-p', [string]$bootstrap.rescue.port,
            '-R', ('127.0.0.1:{0}:127.0.0.1:22' -f $bootstrap.rescue.reverse_port),
            '-o', 'ExitOnForwardFailure=yes',
            '-o', ('ServerAliveInterval={0}' -f $bootstrap.rescue.keepalive_interval_seconds),
            '-o', ('ServerAliveCountMax={0}' -f $bootstrap.rescue.keepalive_count_max),
            '-o', 'StrictHostKeyChecking=yes',
            '-o', ('UserKnownHostsFile={0}' -f $RescueKnown),
            '-o', 'IdentitiesOnly=yes',
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15',
            ('{0}@{1}' -f $bootstrap.rescue.user, $bootstrap.rescue.host)
        )
        $argLiteral = ($sshArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ', '
        $retry = [int]$bootstrap.rescue.retry_seconds
        $runner = @"
# Generated by Terminal MCP windows-setup.ps1 -- rescue reverse-SSH tunnel.
# Runs as SYSTEM from a Scheduled Task at machine startup, before any login.
# ExitOnForwardFailure means ssh EXITS when the forward cannot bind, so this
# loop is what makes the tunnel self-healing: reconnect, forever, with a
# bounded backoff so a permanently-down gateway does not spin the CPU.
`$ErrorActionPreference = 'Continue'
`$log = Join-Path '$LogDir' 'rescue-tunnel.log'
`$sshArgs = @($argLiteral)
`$backoff = $retry
while (`$true) {
    `$started = Get-Date
    try {
        & ssh.exe @sshArgs 2>&1 | ForEach-Object {
            "`$(Get-Date -Format o)  `$_" | Add-Content -Path `$log
        }
    } catch {
        "`$(Get-Date -Format o)  launch failed: `$(`$_.Exception.Message)" | Add-Content -Path `$log
    }
    `$lived = (New-TimeSpan -Start `$started -End (Get-Date)).TotalSeconds
    if (`$lived -ge 120) { `$backoff = $retry } else { `$backoff = [Math]::Min(`$backoff * 2, 300) }
    "`$(Get-Date -Format o)  tunnel exited after `$([int]`$lived)s; retrying in `$backoff s" |
        Add-Content -Path `$log
    # Keep the log bounded -- this runs forever.
    try {
        if ((Get-Item `$log -ErrorAction SilentlyContinue).Length -gt 5MB) {
            Get-Content `$log -Tail 2000 | Set-Content "`$log.tmp"; Move-Item "`$log.tmp" `$log -Force
        }
    } catch { }
    Start-Sleep -Seconds `$backoff
}
"@
        Set-Content -Path $RescueRunner -Value $runner -Encoding utf8

        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RescueRunner`""
        # AtStartup + SYSTEM is what makes this work before any user logs in
        # and across every reboot -- an -AtLogOn task would leave a headless
        # machine unreachable until somebody signed in.
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskRescue -Action $action -Trigger $trigger -Principal $principal `
            -Settings $settings -Description "Terminal MCP rescue reverse-SSH tunnel for $NodeId" -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskRescue
        $synced = $true
        if ($bootstrap.rescue.PSObject.Properties.Name -contains 'authorized_keys_synced') {
            $synced = [bool]$bootstrap.rescue.authorized_keys_synced
        }
        if ($synced) {
            Add-Step 'Rescue tunnel' 'OK' ("reverse port {0} on {1}; task runs at startup as SYSTEM" -f `
                $bootstrap.rescue.reverse_port, $bootstrap.rescue.host)
        } else {
            Add-Step 'Rescue tunnel' 'WARN' 'tunnel task installed, but the gateway has not accepted this key yet' `
                'An admin must install this node''s line into the gateway authorized_keys (see the Dashboard)'
        }
    } catch {
        Add-Step 'Rescue tunnel' 'FAIL' $_.Exception.Message
    }
}

# ===========================================================================
#  7. Heartbeat -- what makes the node show Ready on the Dashboard
# ===========================================================================
End-Stage
Start-Stage 'Heartbeat (de node hien tren Dashboard)'
if (-not (Test-Path $TokenFile)) {
    Add-Step 'Heartbeat task' 'FAIL' 'no node token on disk (enrollment did not complete)'
} else {
    try {
        $interval = 30
        if ($bootstrap -and $bootstrap.heartbeat_interval_seconds) {
            $interval = [int]$bootstrap.heartbeat_interval_seconds
        }
        $beat = @"
# Generated by Terminal MCP windows-setup.ps1 -- node heartbeat.
# Pure PowerShell, no dependencies: this is what makes the node visible on
# the Dashboard even on a Minimal profile with nothing else installed.
`$ErrorActionPreference = 'Continue'
`$cfg = Get-Content '$NodeConfig' -Raw | ConvertFrom-Json
`$token = (Get-Content '$TokenFile' -Raw).Trim()
`$log = Join-Path '$LogDir' 'heartbeat.log'
`$nodeId = '$NodeId'
`$uri = "`$(`$cfg.controller_url)/dashboard/api/nodes/`$nodeId/heartbeat"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11

function Get-Metrics {
    `$os = Get-CimInstance Win32_OperatingSystem
    `$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    `$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
    `$ramTotal = [int64]`$os.TotalVisibleMemorySize * 1024
    `$ramFree = [int64]`$os.FreePhysicalMemory * 1024
    `$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
    `$diskTotal = [int64]`$disk.Size
    `$diskFree = [int64]`$disk.FreeSpace
    return @{
        cpu_percent = [double](`$cpu | ForEach-Object { `$_ })
        load1 = `$null; load5 = `$null; load15 = `$null
        cpu_count = [int]`$cores
        ram_total_bytes = `$ramTotal
        ram_used_bytes = `$ramTotal - `$ramFree
        ram_percent = if (`$ramTotal -gt 0) { [math]::Round(100.0 * (`$ramTotal - `$ramFree) / `$ramTotal, 1) } else { `$null }
        swap_total_bytes = `$null; swap_used_bytes = `$null; swap_percent = `$null
        disk_total_bytes = `$diskTotal
        disk_used_bytes = `$diskTotal - `$diskFree
        disk_free_bytes = `$diskFree
        disk_percent = if (`$diskTotal -gt 0) { [math]::Round(100.0 * (`$diskTotal - `$diskFree) / `$diskTotal, 1) } else { `$null }
    }
}

function Get-AgentTypes {
    `$found = @()
    foreach (`$pair in @(@('claude','claude'), @('codex','codex'), @('opencode','opencode'))) {
        if (Get-Command `$pair[1] -ErrorAction SilentlyContinue) { `$found += `$pair[0] }
    }
    return `$found
}

function Get-Capabilities {
    `$found = @()
    foreach (`$pair in @(@('git','git'), @('python','python'), @('node','node'), @('gh','gh'),
                        @('pwsh','pwsh'), @('tailscale','tailscale'))) {
        if (Get-Command `$pair[1] -ErrorAction SilentlyContinue) { `$found += `$pair[0] }
    }
    `$rescue = Get-ScheduledTask -TaskName '$TaskRescue' -ErrorAction SilentlyContinue
    if (`$rescue) {
        if (`$rescue.State -eq 'Running') { `$found += 'rescue_tunnel_running' } else { `$found += 'rescue_tunnel_stopped' }
    }
    `$sshd = Get-Service sshd -ErrorAction SilentlyContinue
    if (`$sshd -and `$sshd.Status -eq 'Running') { `$found += 'sshd_running' }
    return `$found
}

while (`$true) {
    try {
        `$payload = @{
            metrics = Get-Metrics
            tmux_session_count = 0
            agent_counts = @{}
            agent_types = Get-AgentTypes
            agent_version = 'windows-setup/$ScriptVersion'
            labels = @('windows', 'onboarded', 'profile:$ProfileName')
            platform = 'windows'
            session_backend = 'windows_pty'
            shell_capabilities = @('powershell')
            capabilities = Get-Capabilities
            wsl_available = [bool](Get-Command wsl -ErrorAction SilentlyContinue)
        } | ConvertTo-Json -Depth 6
        Invoke-RestMethod -Method Post -Uri `$uri -Headers @{ Authorization = "Bearer `$token" } ``
            -ContentType 'application/json' -Body `$payload -TimeoutSec 20 -UseBasicParsing | Out-Null
    } catch {
        "`$(Get-Date -Format o)  heartbeat failed: `$(`$_.Exception.Message)" | Add-Content -Path `$log
        try {
            if ((Get-Item `$log -ErrorAction SilentlyContinue).Length -gt 2MB) {
                Get-Content `$log -Tail 1000 | Set-Content "`$log.tmp"; Move-Item "`$log.tmp" `$log -Force
            }
        } catch { }
    }
    Start-Sleep -Seconds $interval
}
"@
        Set-Content -Path $BeatRunner -Value $beat -Encoding utf8
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$BeatRunner`""
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskBeat -Action $action -Trigger $trigger -Principal $principal `
            -Settings $settings -Description "Terminal MCP heartbeat for $NodeId" -Force | Out-Null
        Restart-ScheduledTask -TaskName $TaskBeat -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskBeat -ErrorAction SilentlyContinue
        Add-Step 'Heartbeat task' 'OK' "every ${interval}s, at startup as SYSTEM"
    } catch {
        Add-Step 'Heartbeat task' 'FAIL' $_.Exception.Message
    }
}

# ===========================================================================
#  Profile packages
# ===========================================================================
End-Stage
Start-Stage ("Cai cong cu theo profile: {0}" -f $ProfileName)
if ($WingetPackages.Count -gt 0 -or $NpmTools.Count -gt 0) {
    Report-Stage 'installing_tools'
    if ($SkipProfilePackages) {
        Add-Step 'Profile packages' 'SKIP' '-SkipProfilePackages given'
    } elseif (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Add-Step 'Profile packages' 'WARN' 'winget is not available on this machine' `
            'Install App Installer from the Microsoft Store, then re-run with -Repair'
    } else {
        foreach ($pkg in $WingetPackages) {
            if (Get-Command $pkg.Probe -ErrorAction SilentlyContinue) {
                Add-Step $pkg.Name 'OK' 'already installed'
                continue
            }
            try {
                Write-Host ("      dang cai {0} ..." -f $pkg.Name) -ForegroundColor DarkGray
                $id = $pkg.Id
                Invoke-Tracked -What ("Cai " + $pkg.Name) -Stage 'installing_tools' `
                    -SlowAfterSeconds 300 -TimeoutSeconds 1800 `
                    -Work ([scriptblock]::Create("& winget install --id $id --silent --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1"))
                # winget puts new binaries on the machine PATH; this process
                # still has the old one, so re-read it before probing.
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                            [Environment]::GetEnvironmentVariable('Path', 'User')
                if (Get-Command $pkg.Probe -ErrorAction SilentlyContinue) {
                    Add-Step $pkg.Name 'OK' 'installed'
                } else {
                    Add-Step $pkg.Name 'WARN' 'winget reported success but the command is not on PATH yet' `
                        'Usually a fresh shell fixes this; otherwise re-run with -Repair after signing out'
                }
            } catch {
                Add-Step $pkg.Name 'WARN' $_.Exception.Message
            }
        }
        foreach ($tool in $NpmTools) {
            if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
                Add-Step $tool.Name 'WARN' 'npm not available -- Node.js install did not complete'
                continue
            }
            if (Get-Command $tool.Probe -ErrorAction SilentlyContinue) {
                Add-Step $tool.Name 'OK' 'already installed (sign-in still required)'
                continue
            }
            try {
                Write-Host ("      dang cai {0} ..." -f $tool.Name) -ForegroundColor DarkGray
                $tid = $tool.Id
                Invoke-Tracked -What ("Cai " + $tool.Name) -Stage 'installing_tools' `
                    -SlowAfterSeconds 240 -TimeoutSeconds 1200 `
                    -Work ([scriptblock]::Create("& npm install -g $tid 2>&1"))
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                            [Environment]::GetEnvironmentVariable('Path', 'User')
                if (Get-Command $tool.Probe -ErrorAction SilentlyContinue) {
                    Add-Step $tool.Name 'OK' 'installed -- run it once on this machine to sign in'
                } else {
                    Add-Step $tool.Name 'WARN' 'npm install finished but the command is not on PATH yet'
                }
            } catch {
                Add-Step $tool.Name 'WARN' $_.Exception.Message
            }
        }
    }
} else {
    Add-Step 'Profile packages' 'SKIP' 'profile Minimal -- khong cai them gi'
}
End-Stage

# ===========================================================================
#  Verification + summary
# ===========================================================================
Start-Stage 'Kiem tra cuoi'
try {
    $sshd = Get-Service sshd -ErrorAction Stop
    if ($sshd.Status -eq 'Running' -and $sshd.StartType -eq 'Automatic') {
        Add-Step 'Verify: sshd' 'OK' 'Running / Automatic'
    } else {
        Add-Step 'Verify: sshd' 'FAIL' "$($sshd.Status) / $($sshd.StartType)" 'Start-Service sshd'
    }
} catch { Add-Step 'Verify: sshd' 'FAIL' 'service not found' }

foreach ($pair in @(@($TaskBeat, 'Verify: heartbeat task'), @($TaskRescue, 'Verify: rescue task'))) {
    $task = Get-ScheduledTask -TaskName $pair[0] -ErrorAction SilentlyContinue
    if (-not $task) {
        if ($pair[0] -eq $TaskRescue -and -not $rescueConfigured) {
            Add-Step $pair[1] 'SKIP' 'rescue not configured'
        } else {
            Add-Step $pair[1] 'FAIL' 'task not registered'
        }
        continue
    }
    # Persistence across reboot is verified by CONFIGURATION, not by
    # rebooting: an AtStartup trigger with a SYSTEM principal is exactly
    # what makes it come back, and both are readable right here.
    $atStartup = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }).Count -gt 0
    $asSystem = $task.Principal.UserId -match 'SYSTEM'
    if ($atStartup -and $asSystem) {
        Add-Step $pair[1] 'OK' "state=$($task.State), survives reboot (AtStartup, SYSTEM)"
    } else {
        Add-Step $pair[1] 'WARN' "state=$($task.State), AtStartup=$atStartup SYSTEM=$asSystem"
    }
}

if ($bootstrap -and (Test-Path $TokenFile)) {
    try {
        $tok = (Get-Content $TokenFile -Raw).Trim()
        $probe = @{
            metrics = @{}; tmux_session_count = 0; agent_counts = @{}; agent_types = @();
            agent_version = "windows-setup/$ScriptVersion"; labels = @('windows'); platform = 'windows';
            session_backend = 'windows_pty'; shell_capabilities = @('powershell'); capabilities = @();
            wsl_available = $false
        } | ConvertTo-Json -Depth 5
        Invoke-RestMethod -Method Post -Uri "$ControllerUrl/dashboard/api/nodes/$NodeId/heartbeat" `
            -Headers @{ Authorization = "Bearer $tok" } -ContentType 'application/json' `
            -Body $probe -TimeoutSec 20 -UseBasicParsing | Out-Null
        Add-Step 'Verify: controller sees this node' 'OK' 'heartbeat accepted'
    } catch {
        Add-Step 'Verify: controller sees this node' 'FAIL' $_.Exception.Message `
            'Check that this machine can reach the controller URL above'
    }
}

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host ' TONG KET  (PASS / WARN / FAIL theo tung buoc)' -ForegroundColor Cyan
Write-Host '=====================================================' -ForegroundColor Cyan
$fails = @($script:Checklist | Where-Object { $_.State -eq 'FAIL' })
$warns = @($script:Checklist | Where-Object { $_.State -eq 'WARN' })
foreach ($row in $script:Checklist) {
    $color = switch ($row.State) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'DarkGray' } }
    Write-Host ('  {0,-4}  {1}' -f $row.State, $row.Name) -ForegroundColor $color
}
if ($fails.Count -gt 0 -or $warns.Count -gt 0) {
    Write-Host ''
    Write-Host ' What to do next:' -ForegroundColor Yellow
    foreach ($row in ($fails + $warns)) {
        Write-Host ("  - {0}: {1}" -f $row.Name, $row.Detail) -ForegroundColor Yellow
        if ($row.Fix) { Write-Host ("      fix: {0}" -f $row.Fix) -ForegroundColor DarkYellow }
    }
}
Write-Host ''
Write-Host (' Tong thoi gian: {0}s' -f [int]$script:TotalSw.Elapsed.TotalSeconds) -ForegroundColor DarkGray

# The window was opened by UAC from Win+R, so when this script returns the
# window vanishes with it. Anything the user still needs to read has to be
# held on screen deliberately.
function Wait-BeforeClosing {
    param([int] $Seconds = 0, [string] $Prompt = 'Nhan Enter de dong cua so nay')
    if ($Seconds -gt 0) {
        Write-Host ''
        Write-Host (" Cua so se dong sau {0} giay..." -f $Seconds) -ForegroundColor DarkGray
        Start-Sleep -Seconds $Seconds
        return
    }
    Write-Host ''
    try { Read-Host $Prompt | Out-Null } catch { Start-Sleep -Seconds 60 }
}

if ($fails.Count -gt 0) {
    Report-Stage 'failed'
    Write-Host " CHUA XONG -- $($fails.Count) buoc bat buoc that bai." -ForegroundColor Red
    Write-Host " Sua cac muc o tren roi chay lai file nay voi -Repair." -ForegroundColor Red
    # FAIL: never auto-close. The user must be able to read why.
    Wait-BeforeClosing
    exit 1
}
if ($warns.Count -gt 0) {
    Report-Stage 'ready'
    Write-Host " SAN SANG (con canh bao) -- node da dang ky va ket noi duoc, con $($warns.Count) muc tuy chon." -ForegroundColor Yellow
    Write-Host " May se hien tren trang Nodes cua Dashboard trong khoang mot phut." -ForegroundColor Yellow
    # WARN: also hold, the warnings are the whole reason to look.
    Wait-BeforeClosing
    exit 2
}
Report-Stage 'ready'
Write-Host ' SAN SANG -- may nay da la mot Terminal MCP node.' -ForegroundColor Green
Write-Host ' May se hien tren trang Nodes cua Dashboard trong khoang mot phut.' -ForegroundColor Green
Wait-BeforeClosing -Seconds 20
exit 0
"""
