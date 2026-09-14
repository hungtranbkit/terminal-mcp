//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

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
	if err := runCommand("sc.exe", "start", ServiceName); err != nil {
		// Already running is not a failure.
		if !strings.Contains(err.Error(), "1056") {
			return err
		}
	}
	return nil
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
	script := filepath.Join(programDataDir(), "windows-setup.ps1")
	args := []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script}
	if extraArg != "" {
		args = append(args, extraArg)
	}
	command := exec.Command("powershell.exe", args...)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	return command.Run()
}
