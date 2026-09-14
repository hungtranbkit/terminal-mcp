# First-install E2E — on a disposable Windows, never a production node

**Not run yet, and not runnable on `dell-5530`.** That node hosts six live
ConPTY sessions which are in-process children of its agent, so they do not
survive a restart by design — and this procedure installs a LocalSystem
service and registers a machine-wide protocol handler. Its Windows Sandbox,
Hyper-V and VirtualMachinePlatform features are all **Disabled**, and enabling
any of them needs a reboot.

So this is written to be executed on a **disposable** Windows: a Sandbox
session, a throwaway VM, or a machine nobody minds reimaging. Getting one is
the only outstanding requirement; everything else below is mechanical.

## What is actually unproven

Every part of this has unit and integration coverage, and two parts have been
exercised on real Windows (`status`/`version` output, and the no-pairing
fallback message). What has **never** run end to end, even once:

1. the paired file name reaching `runInstall` on a double-click
2. copying into Program Files and creating the service
3. registering `terminalmcp://` machine-wide
4. the service claiming a handle over the named pipe
5. `redeem` returning a real bootstrap payload to a machine that has none
6. the Dashboard's progress strip moving because of a real install

Anything below that fails is a finding, not a step to work around.

## Preparing the disposable host

**Sandbox** (Windows 10/11 Pro or Enterprise) — needs
`Containers-DisposableClientVM` enabled once, which needs a reboot. Note that
a Sandbox session discards everything on close, which is exactly what makes it
safe and also means the install cannot be inspected afterwards: capture output
as you go.

**VM** — any hypervisor. Take a snapshot before step 1 so a failed run is a
revert rather than a rebuild.

Requirements either way: network reach to the controller, and the ability to
click through a UAC prompt.

## The run

Record the output of every step. A pass that nobody wrote down is a pass
nobody can point at later.

1. **Baseline.** On the fresh host, before anything:

       terminal-mcp-bootstrap.exe status --json

   Expect `"installed": false`, `"controllers": null`. If it says otherwise the
   host is not fresh and the run is invalid.

2. **Create a session.** On the Dashboard, Nodes → generate setup for a node
   name used only for this test. Do not reuse a real node's name: a half
   finished enrollment under a live node's identity is the one outcome worth
   avoiding.

3. **Download via the CTA.** Press the download button, not a copied URL — the
   point is that the CTA pairs the file. Check the saved name before running
   it:

       terminal-mcp-bootstrap__<origin>__<32 hex>.exe

   If the name is plain, the browser stripped it. That is a supported
   fallback, but it means this run is testing the fallback, not the paired
   path. Note which one you got.

4. **Double-click. Accept UAC.** From here nothing should need typing. Expect
   the controller origin printed, and the handle **not** printed.

5. **Watch the Dashboard.** The progress strip should move on its own. If it
   does not, the install may still be succeeding — check step 6 before
   concluding anything.

6. **Verify on the host:**

       terminal-mcp-bootstrap.exe status --json     # installed: true, controller set
       sc query TerminalMCPBootstrap                # RUNNING
       reg query HKCR\terminalmcp                   # handler registered

7. **Verify from the controller:** the node appears in the registry, reports a
   heartbeat, and the enrollment is `consumed` rather than `pending`.

8. **Second click.** Back on the Dashboard, press "Kết nối máy này" once more.
   It should be a no-op that reports the helper already installed — not a
   second install, and not an error.

## Teardown

    terminal-mcp-bootstrap.exe uninstall

Removes what the helper installed and nothing else. Then close the Sandbox or
revert the snapshot, and revoke the test node on the controller so it does not
sit in the registry as a machine that no longer exists.

## Recording the result

A run is only evidence if it says which build it exercised:

- helper version and build SHA from step 1
- artifact SHA256 (the `X-Artifact-Sha256` header on the download)
- whether the name arrived paired or plain
- which of the six unproven steps passed, individually — not "it worked"

## Known limitations of this procedure

- **Unsigned.** SmartScreen will warn at step 4, and clicking through it is
  part of the test. That is the current state, stated plainly; it is not
  something to coach a real operator past.
- **Sandbox has no TPM and no persistent identity.** Anything that later
  depends on machine identity across reboots cannot be proven there. A VM with
  a snapshot can.
- **One machine proves one path.** A domain-joined machine, a machine with an
  EDR agent, and a machine behind a proxy that rewrites downloads will each
  fail differently, and none of them are covered by this.
