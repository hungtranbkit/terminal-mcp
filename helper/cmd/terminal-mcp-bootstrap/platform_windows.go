//go:build windows

package main

import (
	"context"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/hungtranbkit/terminal-mcp/helper/internal/proto"
	"golang.org/x/sys/windows/registry"
)

// registerProtocol writes the terminalmcp:// handler under HKLM, so it
// applies machine-wide rather than per-user. The command line is a FIXED
// shape -- `"<exe>" "%1"` -- and %1 lands in os.Args[1], which goes
// straight to the validator. There is no shell in that path.
func registerProtocol() error {
	key, _, err := registry.CreateKey(registry.LOCAL_MACHINE,
		`SOFTWARE\Classes\`+proto.Scheme, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("protocol key: %w", err)
	}
	defer key.Close()
	if err := key.SetStringValue("", "URL:Terminal MCP Bootstrap"); err != nil {
		return err
	}
	if err := key.SetStringValue("URL Protocol", ""); err != nil {
		return err
	}
	command, _, err := registry.CreateKey(registry.LOCAL_MACHINE,
		`SOFTWARE\Classes\`+proto.Scheme+`\shell\open\command`, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("command key: %w", err)
	}
	defer command.Close()
	return command.SetStringValue("", `"`+installedBinaryPath()+`" "%1"`)
}

func unregisterProtocol() error {
	// DeleteKey needs the leaves first.
	for _, path := range []string{
		`SOFTWARE\Classes\` + proto.Scheme + `\shell\open\command`,
		`SOFTWARE\Classes\` + proto.Scheme + `\shell\open`,
		`SOFTWARE\Classes\` + proto.Scheme + `\shell`,
		`SOFTWARE\Classes\` + proto.Scheme,
	} {
		_ = registry.DeleteKey(registry.LOCAL_MACHINE, path)
	}
	return nil
}

// installService uses sc.exe rather than linking a service-install
// library: fewer moving parts, and the exact same command an operator
// would run by hand when diagnosing.
func installService() error {
	binary := installedBinaryPath()
	// Idempotent: reconfigure an existing service instead of failing.
	if serviceExists() {
		if err := runCommand("sc.exe", "config", ServiceName,
			"binPath=", fmt.Sprintf("%q service", binary), "start=", "auto"); err != nil {
			return err
		}
	} else if err := runCommand("sc.exe", "create", ServiceName,
		"binPath=", fmt.Sprintf("%q service", binary),
		"start=", "auto", "obj=", "LocalSystem",
		"DisplayName=", "Terminal MCP Bootstrap"); err != nil {
		return err
	}
	_ = runCommand("sc.exe", "description", ServiceName,
		"Terminal MCP Bootstrap -- onboarding and repair for this node.")

	// Already up? Then "install and start" is already achieved. Asking SCM
	// to start a RUNNING service is how a Repair or a second enrollment on
	// the same machine used to turn into a failure for no reason.
	if state := serviceState(); serviceStateIsUsable(state) {
		fmt.Printf("      service already %s\n", state)
		return nil
	}

	// Bounded. Before the SCM handshake existed this call blocked for
	// SCM's full ServicesPipeTimeout with nothing on screen after "[3/3]";
	// the timeout is the backstop that guarantees the installer moves on
	// even if a future change breaks the handshake again.
	if err := runCommandTimeout(serviceStartTimeout, "sc.exe", "start", ServiceName); err != nil {
		// 1056 is "already running", which is success wearing an error.
		if strings.Contains(err.Error(), "1056") {
			return nil
		}
		// A start that timed out or failed is reported with what to do
		// about it, not just an error code. The pairing is untouched, so
		// retrying costs nothing.
		return fmt.Errorf("%w -- kiểm tra bằng: sc.exe query %s", err, ServiceName)
	}
	return nil
}

// serviceStartTimeout matches ServicesPipeTimeout's 30s default: long
// enough that a healthy service is never cut off, short enough that a
// broken one cannot hold the installer open indefinitely.
const serviceStartTimeout = 30 * time.Second

// restrictTokenACL applies the SAME ACL the installer applies to
// node.token: inheritance broken, read for SYSTEM and Administrators, and
// nothing for anyone else. Go's chmod cannot express this on Windows -- it
// only toggles the read-only bit -- which is why the secret must not live
// in a file protected by mode alone.
func restrictTokenACL(path string) error {
	return runCommandTimeout(15*time.Second, "icacls.exe", path,
		"/inheritance:r", "/grant", "SYSTEM:(R)", "/grant", "Administrators:(R)")
}

// serviceState returns SCM's own word for the service state ("RUNNING",
// "STOPPED", ...) or "" when it cannot be determined. Read-only: it never
// creates or changes anything, so it is safe to call before deciding.
func serviceState() string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, "sc.exe", "query", ServiceName).CombinedOutput()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(output), "\n") {
		if !strings.Contains(line, "STATE") {
			continue
		}
		for _, state := range []string{
			"STOPPED", "START_PENDING", "STOP_PENDING", "RUNNING",
			"CONTINUE_PENDING", "PAUSE_PENDING", "PAUSED",
		} {
			if strings.Contains(line, state) {
				return state
			}
		}
	}
	return ""
}

func serviceExists() bool {
	return exec.Command("sc.exe", "query", ServiceName).Run() == nil
}

func removeService() error {
	_ = runCommand("sc.exe", "stop", ServiceName)
	return runCommand("sc.exe", "delete", ServiceName)
}

// runStages delegates the actual onboarding to windows-setup.ps1, which
// is already tested, deployed and hardened. Rewriting those stages in Go
// would double the surface and halve the confidence.
func runStages(controller, extraArg string) error {
	_, err := runStagesBounded(controller, extraArg, installerTimeout)
	return err
}

// runStagesBounded runs windows-setup.ps1 and returns its EXIT CODE.
//
// Three things it fixes beyond the timeout:
//
//	stdin   the child used to inherit no stdin at all, so the script's
//	        Wait-BeforeClosing did `Read-Host`, threw, and fell into its
//	        `catch { Start-Sleep -Seconds 60 }`. A helper that is not a
//	        human waited a silent minute on every failure. Feeding it
//	        newlines makes Read-Host return at once on every path.
//	exit    the code is returned rather than flattened to error/no-error,
//	        so the caller can report WHICH failure happened.
//	output  stdout/stderr still go to this console for the operator, and
//	        are deliberately NOT captured for upload: the installer prints
//	        controller URLs, key material paths and other things that have
//	        no business in a progress report.
func runStagesBounded(controller, extraArg string, limit time.Duration) (int, error) {
	script := filepath.Join(programDataDir(), "windows-setup.ps1")
	args := []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script}
	if extraArg != "" {
		args = append(args, extraArg)
	}
	return runBoundedCommand(limit, newlineFeeder(), "powershell.exe", args...)
}
