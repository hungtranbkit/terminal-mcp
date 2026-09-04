#Requires -Version 5.1
<#
.SYNOPSIS
  Configures a Windows terminal-mcp worker node (installed via
  install-node-agent.ps1) for stable, unattended 24/7 operation -- power
  settings that keep it awake/reachable on AC power, Scheduled Task
  persistence that survives a reboot with NO interactive logon required,
  and a few network/update settings that commonly cause a worker to go
  to sleep or drop off the LAN unexpectedly. Run this ON THE WINDOWS NODE
  ITSELF (or via an already-open admin PowerShell/SSH session to it),
  AFTER install-node-agent.ps1 has already set up the Scheduled Task.

.DESCRIPTION
  Idempotent: every section reads the CURRENT value first and only
  changes it if it doesn't already match the target -- safe to re-run,
  and the log clearly distinguishes "already correct" from "changed" so
  a re-run's output stays meaningful. Never touches a secret (this
  script has no knowledge of the node's own bearer token at all -- see
  install-node-agent.ps1/run-node-agent.ps1 for that).

  Deliberately conservative in a few places, matching this task's own
  explicit safety requirements:
    - Only ever touches AC (plugged-in) power settings -- DC (battery)
      policy is never modified, so unplugging this machine still behaves
      exactly as before.
    - NEVER touches the SSH inbound firewall rule(s) or the sshd service's
      own listening configuration -- the single highest-risk change this
      script could make is accidentally narrowing/breaking the very
      access path being used to run it. It only VERIFIES and reports
      sshd's current state; any firewall-scope tightening there is a
      separate, deliberate, human-reviewed decision (docs/multi-node.md
      already covers narrowing to a specific LAN CIDR if wanted).
    - The Scheduled Task's own run-as identity is changed from
      LogonType=Interactive (requires an actual interactive logon
      session -- the reason it previously only started when someone
      logged in) to LogonType=S4U, which still runs as the SAME
      unprivileged user (never SYSTEM/elevated -- an agent session
      spawned under SYSTEM would hand anyone with dashboard access to
      this node SYSTEM-level shell access, a real privilege escalation
      this script must never introduce) but no longer needs anyone
      logged in at all. An AtStartup trigger is added alongside the
      existing AtLogOn one (both can fire; MultipleInstances=IgnoreNew
      already prevents a double-launch).
    - Windows Update is never disabled -- only Active Hours (a normal,
      supported, non-destructive Windows setting) is verified/set to a
      wide daily window, reducing the odds of an unexpected mid-task
      auto-reboot without touching the update mechanism itself.

.PARAMETER NodeAgentTaskName
  The Scheduled Task name install-node-agent.ps1 registered (default
  matches its own convention: "TerminalMcpNodeAgent-<NodeId>").

.PARAMETER ActiveHoursStart / ActiveHoursEnd
  Windows Update Active Hours window (24h clock) -- default 6-23.

.EXAMPLE
  .\configure-windows-node-stability.ps1 -NodeAgentTaskName TerminalMcpNodeAgent-dell-5530

.NOTES
  Run from an elevated (Administrator) PowerShell / SSH session -- most
  sections below no-op with a clear log line (never a silent partial
  application) if not elevated; re-run elevated to complete those.
#>
[CmdletBinding()]
param(
    [string] $NodeAgentTaskName = "TerminalMcpNodeAgent",
    [int] $ActiveHoursStart = 6,
    [int] $ActiveHoursEnd = 23
)

$ErrorActionPreference = "Stop"
$changes = New-Object System.Collections.Generic.List[string]
$skipped = New-Object System.Collections.Generic.List[string]

function Log-Change([string]$msg) { Write-Host "  [CHANGED] $msg" -ForegroundColor Yellow; $changes.Add($msg) | Out-Null }
function Log-Ok([string]$msg) { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Log-Skip([string]$msg) { Write-Host "  [SKIPPED] $msg" -ForegroundColor DarkGray; $skipped.Add($msg) | Out-Null }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
Write-Host "== terminal-mcp Windows node stability configuration ==" -ForegroundColor Cyan
Write-Host "  Running as: $([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-Host "  Administrator: $isAdmin"
Write-Host ""

if (-not $isAdmin) {
    Write-Warning "Not running elevated -- power/task/firewall sections below need Administrator. Re-run this script from an elevated PowerShell/SSH session to complete them; user-level checks (read-only reporting) still run."
}

# -- 1/2. Power settings (AC only) -- sleep/hibernate/monitor never, on AC --
Write-Host "== Power settings (AC only -- battery/DC policy untouched) ==" -ForegroundColor Cyan
function Get-AcPowerValue([string]$subgroup, [string]$setting) {
    # Not every subgroup/setting alias is exposed on every scheme/hardware
    # (e.g. no USB subgroup on some machines) -- powercfg then exits non-zero
    # with "Invalid Parameters", which must be treated as "not present here",
    # not a fatal script error.
    try {
        $raw = & powercfg /query SCHEME_CURRENT $subgroup $setting 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = $raw | Select-String "Current AC Power Setting Index:\s+0x([0-9a-fA-F]+)"
    if ($line) { return [Convert]::ToInt64($line.Matches[0].Groups[1].Value, 16) }
    return $null
}
function Ensure-AcNever([string]$subgroup, [string]$setting, [string]$label) {
    if (-not $isAdmin) { Log-Skip "$label -- needs Administrator"; return }
    $current = Get-AcPowerValue $subgroup $setting
    if ($current -eq 0) { Log-Ok "$label already Never on AC"; return }
    powercfg /setacvalueindex SCHEME_CURRENT $subgroup $setting 0 2>&1 | Out-Null
    powercfg /setactive SCHEME_CURRENT 2>&1 | Out-Null
    $after = Get-AcPowerValue $subgroup $setting
    if ($after -eq 0) { Log-Change "$label set to Never on AC (was $current)" }
    else { Log-Skip "$label -- setting not supported on this hardware/scheme (no effect after attempting)" }
}
Ensure-AcNever "SUB_SLEEP" "STANDBYIDLE" "Sleep after"
Ensure-AcNever "SUB_SLEEP" "HIBERNATEIDLE" "Hibernate after"
Ensure-AcNever "SUB_VIDEO" "VIDEOIDLE" "Turn off display after"
# Lid close action -- only present on hardware/drivers that expose a lid
# sensor; many docked/desktop-mode or some laptop driver combos don't
# surface this setting at all, in which case powercfg's own attempt is a
# harmless no-op (nothing to set, nothing to break).
if ($isAdmin) {
    $lidBefore = Get-AcPowerValue "SUB_BUTTONS" "LIDACTION"
    if ($null -eq $lidBefore) {
        Log-Skip "Lid close action (AC) -- no lid-action setting exposed by this hardware/driver, nothing to configure"
    } elseif ($lidBefore -eq 0) {
        Log-Ok "Lid close action (AC) already 'Do nothing'"
    } else {
        powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 2>&1 | Out-Null
        powercfg /setactive SCHEME_CURRENT 2>&1 | Out-Null
        Log-Change "Lid close action (AC) set to 'Do nothing' (was $lidBefore)"
    }
}

# -- 6. NIC power saving + USB selective suspend (AC) ------------------------
Write-Host "== Network adapter / USB power saving ==" -ForegroundColor Cyan
if ($isAdmin) {
    Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object {
        $adapterName = $_.Name
        try {
            $pm = Get-NetAdapterPowerManagement -Name $adapterName -ErrorAction Stop
            if ($pm.AllowComputerToTurnOffDevice -eq "Disabled" -or $pm.AllowComputerToTurnOffDevice -eq "Unsupported") {
                Log-Ok "NIC '$adapterName' power saving already off/unsupported ($($pm.AllowComputerToTurnOffDevice))"
            } else {
                Set-NetAdapterPowerManagement -Name $adapterName -AllowComputerToTurnOffDevice Disabled -ErrorAction Stop
                Log-Change "NIC '$adapterName' 'allow the computer to turn off this device' disabled"
            }
        } catch {
            Log-Skip "NIC '$adapterName' power management -- not exposed by this driver ($($_.Exception.Message))"
        }
    }
    $usbBefore = Get-AcPowerValue "SUB_USB" "USBSELECTSUSPEND"
    if ($null -eq $usbBefore) {
        Log-Skip "USB selective suspend (AC) -- setting not present on this scheme"
    } elseif ($usbBefore -eq 0) {
        Log-Ok "USB selective suspend (AC) already disabled"
    } else {
        powercfg /setacvalueindex SCHEME_CURRENT SUB_USB USBSELECTSUSPEND 0 2>&1 | Out-Null
        powercfg /setactive SCHEME_CURRENT 2>&1 | Out-Null
        Log-Change "USB selective suspend (AC) disabled (was $usbBefore)"
    }
} else {
    Log-Skip "NIC/USB power settings -- need Administrator"
}

# -- 6. Windows Update Active Hours (never disables Update itself) -----------
Write-Host "== Windows Update Active Hours ==" -ForegroundColor Cyan
if ($isAdmin) {
    $auPath = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    if (-not (Test-Path $auPath)) { New-Item -Path $auPath -Force | Out-Null }
    $current = Get-ItemProperty -Path $auPath -ErrorAction SilentlyContinue
    $curStart = $current.ActiveHoursStart
    $curEnd = $current.ActiveHoursEnd
    if ($curStart -eq $ActiveHoursStart -and $curEnd -eq $ActiveHoursEnd) {
        Log-Ok "Active Hours already $ActiveHoursStart-$ActiveHoursEnd"
    } else {
        Set-ItemProperty -Path $auPath -Name ActiveHoursStart -Value $ActiveHoursStart -Type DWord
        Set-ItemProperty -Path $auPath -Name ActiveHoursEnd -Value $ActiveHoursEnd -Type DWord
        Set-ItemProperty -Path $auPath -Name IsActiveHoursEnabled -Value 1 -Type DWord
        Log-Change "Active Hours set to $ActiveHoursStart-$ActiveHoursEnd (was $curStart-$curEnd) -- Windows Update itself is untouched, this only avoids an auto-reboot inside this window"
    }
} else {
    Log-Skip "Windows Update Active Hours -- needs Administrator"
}

# -- 3/4. Node agent Scheduled Task: startup trigger + login-independent ----
Write-Host "== Node agent Scheduled Task persistence ==" -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName $NodeAgentTaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Log-Skip "Task '$NodeAgentTaskName' not found -- run install-node-agent.ps1 first"
} else {
    $principal = $task.Principal
    $alreadyS4U = $principal.LogonType -eq "S4U"
    $hasStartupTrigger = $task.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskBootTrigger" }

    if ($alreadyS4U -and $hasStartupTrigger) {
        Log-Ok "Task already login-independent (LogonType=S4U) with an AtStartup trigger"
    } elseif (-not $isAdmin) {
        Log-Skip "Task reconfiguration (LogonType/AtStartup trigger) -- needs Administrator"
    } else {
        $userId = $principal.UserId
        if (-not $userId) { $userId = "$env:COMPUTERNAME\$env:USERNAME" }
        # Keep running as the SAME unprivileged user -- NEVER SYSTEM. An
        # agent session spawned under SYSTEM would hand anyone with
        # dashboard access to this node SYSTEM-level shell access.
        $newPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
        $triggers = @($task.Triggers)
        if (-not $hasStartupTrigger) {
            $triggers += New-ScheduledTaskTrigger -AtStartup
        }
        Set-ScheduledTask -TaskName $NodeAgentTaskName -Principal $newPrincipal -Trigger $triggers | Out-Null
        Log-Change "Task '$NodeAgentTaskName' set to LogonType=S4U (was $($principal.LogonType), still runs as $userId, never SYSTEM) + AtStartup trigger added -- now starts on boot with no interactive logon required"
    }

    $settings = $task.Settings
    if ($settings.RestartCount -ge 3 -and $settings.RestartInterval) {
        Log-Ok "Restart-on-failure already configured (RestartCount=$($settings.RestartCount), RestartInterval=$($settings.RestartInterval))"
    } elseif ($isAdmin) {
        $newSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 0 = no time limit -- this must run indefinitely
        Set-ScheduledTask -TaskName $NodeAgentTaskName -Settings $newSettings | Out-Null
        Log-Change "Restart-on-failure strengthened: RestartCount=5, RestartInterval=1min, no execution time limit"
    } else {
        Log-Skip "Restart-on-failure tuning -- needs Administrator"
    }
}

# -- 5. sshd -- VERIFY ONLY, never modified (see this script's own header) --
Write-Host "== OpenSSH Server (verify only -- never modified by this script) ==" -ForegroundColor Cyan
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd) {
    if ($sshd.StartType -eq "Automatic") { Log-Ok "sshd StartType already Automatic" }
    else { Log-Skip "sshd StartType is '$($sshd.StartType)', not Automatic -- NOT changed automatically (review manually: 'Set-Service sshd -StartupType Automatic')" }
    Write-Host "  sshd status: $($sshd.Status)"
} else {
    Write-Host "  sshd service not found on this host."
}
Get-NetFirewallRule -DisplayName "*ssh*" -ErrorAction SilentlyContinue | ForEach-Object {
    $addr = $_ | Get-NetFirewallAddressFilter
    Write-Host "  Firewall rule '$($_.DisplayName)': Enabled=$($_.Enabled) Profile=$($_.Profile) RemoteAddress=$($addr.RemoteAddress) (not modified by this script)"
}

# -- Summary ------------------------------------------------------------------
Write-Host ""
Write-Host "== Summary ==" -ForegroundColor Cyan
Write-Host "  Changes made: $($changes.Count)"
$changes | ForEach-Object { Write-Host "    - $_" }
Write-Host "  Skipped (already correct, or needs Administrator/unsupported): $($skipped.Count)"
$skipped | ForEach-Object { Write-Host "    - $_" }
if (-not $isAdmin -and $skipped.Count -gt 0) {
    Write-Host ""
    Write-Warning "Re-run this script from an elevated (Administrator) PowerShell/SSH session to complete the skipped sections above."
}
