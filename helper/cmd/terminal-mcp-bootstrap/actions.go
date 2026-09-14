package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/hungtranbkit/terminal-mcp/helper/internal/proto"
)

// newMachineID is a helper-generated UUID persisted under ProgramData.
// Deliberately NOT a hardware fingerprint and never the hostname alone:
// hostnames collide and get renamed, and fingerprinting is both fragile
// and more identifying than this needs to be. It exists so a re-run can
// recognise "this is the same machine" and Repair instead of creating a
// duplicate node.
func newMachineID() string {
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return fmt.Sprintf("fallback-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buffer)
}

// runInstall is idempotent: an existing install is verified and updated in
// place rather than duplicated. Re-running it is the Repair path.
func runInstall(controller, session string) int {
	if controller == "" {
		fmt.Fprintln(os.Stderr, "install requires -controller")
		return 2
	}
	origin, err := proto.NormalizeOrigin(controller)
	if err != nil {
		fmt.Fprintf(os.Stderr, "invalid -controller: %v\n", err)
		return 2
	}

	if err := os.MkdirAll(programDataDir(), 0o700); err != nil {
		fmt.Fprintf(os.Stderr, "cannot create %s: %v\n", programDataDir(), err)
		return 1
	}
	if err := os.MkdirAll(programFilesDir(), 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "cannot create %s: %v\n", programFilesDir(), err)
		return 1
	}

	// Preserve the machine identity across re-installs: losing it is what
	// would turn a Repair into a duplicate node.
	config := Config{Controllers: []string{origin}, Version: Version,
		InstalledAt: time.Now().UTC().Format(time.RFC3339), MachineID: newMachineID()}
	if existing, err := loadConfig(); err == nil {
		if existing.MachineID != "" {
			config.MachineID = existing.MachineID
		}
		// Binding to a second controller is a deliberate act, not a
		// side effect of running install again.
		config.Controllers = mergeControllers(existing.Controllers, origin)
	}
	raw, _ := json.MarshalIndent(config, "", "  ")
	if err := writeAtomic(configPath(), raw, 0o600); err != nil {
		fmt.Fprintf(os.Stderr, "cannot write config: %v\n", err)
		return 1
	}

	steps := []struct {
		name string
		run  func() error
	}{
		{"copy binary to Program Files", func() error { return copySelfTo(installedBinaryPath()) }},
		{"register terminalmcp:// protocol", registerProtocol},
		{"install and start the service", installService},
	}
	degraded := false
	for index, step := range steps {
		fmt.Printf("[%d/%d] %s\n", index+1, len(steps), step.name)
		if err := step.run(); err != nil {
			fmt.Fprintf(os.Stderr, "      ! %v\n", err)
			degraded = true
		}
	}
	if session != "" {
		fmt.Printf("continuing pending session %s\n", session)
		if err := continueSession(session, origin); err != nil {
			fmt.Fprintf(os.Stderr, "      ! %v\n", err)
			degraded = true
		}
	}
	if degraded {
		fmt.Fprintln(os.Stderr, "installed with warnings -- see above")
		return 2
	}
	fmt.Println("Terminal MCP Bootstrap installed.")
	return 0
}

func mergeControllers(existing []string, origin string) []string {
	out := []string{origin}
	for _, entry := range existing {
		if normalized, err := proto.NormalizeOrigin(entry); err == nil && normalized != origin {
			out = append(out, normalized)
		}
	}
	return out
}

func installedBinaryPath() string {
	return filepath.Join(programFilesDir(), "terminal-mcp-bootstrap.exe")
}

// writeAtomic: write-then-rename, so a crash mid-write can never leave a
// half-written credential or config that the service would then read.
func writeAtomic(path string, data []byte, mode os.FileMode) error {
	temp := path + ".tmp"
	if err := os.WriteFile(temp, data, mode); err != nil {
		return err
	}
	return os.Rename(temp, path)
}

func copySelfTo(target string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	if sameFile(self, target) {
		return nil // already running from the installed location
	}
	data, err := os.ReadFile(self)
	if err != nil {
		return err
	}
	return writeAtomic(target, data, 0o755)
}

func sameFile(a, b string) bool {
	infoA, errA := os.Stat(a)
	infoB, errB := os.Stat(b)
	return errA == nil && errB == nil && os.SameFile(infoA, infoB)
}

// runCommand is the single place this helper shells out. Every caller
// passes a fixed argv -- there is no path by which a protocol URL
// contributes to one.
func runCommand(name string, args ...string) error {
	command := exec.Command(name, args...)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s: %v: %s", name, err, trim(string(output)))
	}
	return nil
}

func trim(text string) string {
	if len(text) > 400 {
		return text[:400] + "..."
	}
	return text
}
