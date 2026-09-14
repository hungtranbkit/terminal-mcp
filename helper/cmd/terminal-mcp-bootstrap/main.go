// terminal-mcp-bootstrap -- the Terminal MCP Bootstrap helper.
//
// WHAT THIS DOES, AND DELIBERATELY DOES NOT DO
//
// It owns the things a browser cannot: elevation, a LocalSystem service,
// the terminalmcp:// protocol registration, and machine-wide state under
// ProgramData. It does NOT reimplement onboarding. The stages -- OpenSSH,
// sshd, authorized_keys, firewall, Tailscale, registration, heartbeat,
// profile tools -- already exist in windows-setup.ps1, which is tested,
// deployed and has survived a real production bug fix. Rewriting them in
// Go would double the surface and halve the confidence, so this helper
// fetches that script from the controller it is bound to and runs it.
//
// Modes:
//
//	install    copy to Program Files, register the protocol, create the
//	           service, start it, then continue a pending session
//	service    LocalSystem service; owns the named pipe and runs stages
//	protocol   invoked by terminalmcp://; VALIDATES then forwards
//	status     print what is installed, for humans and for the Dashboard
//	uninstall  remove what this helper installed, and nothing else
//
// The protocol mode never executes anything itself. It validates the URL
// against internal/proto and hands a typed action to the service over a
// named pipe. That separation is the point: the process a web page can
// trigger has no code path that runs a command.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/hungtranbkit/terminal-mcp/helper/internal/proto"
)

// Version is stamped at build time:
//
//	go build -ldflags "-X main.Version=1.0.0 -X main.BuildSHA=$(git rev-parse --short HEAD)"
var (
	Version  = "0.0.0-dev"
	BuildSHA = "unknown"
	// Signed is set to "true" only by a build that actually Authenticode-
	// signs the artifact. It defaults to false so an unsigned dev build
	// can never report itself as signed.
	Signed = "false"
)

const (
	ProductName = "terminal-mcp-bootstrap"
	ServiceName = "TerminalMCPBootstrap"
	PipeName    = `\\.\pipe\TerminalMCPBootstrap`
)

// Machine-wide, never TEMP/AppData/Downloads: a credential that lives in
// a user profile is invisible to the LocalSystem service that needs it,
// which is exactly the class of bug that produced "no node token on disk".
func programFilesDir() string {
	base := os.Getenv("ProgramFiles")
	if base == "" {
		base = `C:\Program Files`
	}
	return filepath.Join(base, "TerminalMCP", "Bootstrap")
}

func programDataDir() string {
	base := os.Getenv("ProgramData")
	if base == "" {
		base = `C:\ProgramData`
	}
	return filepath.Join(base, "TerminalMCP")
}

// Config is the helper's install-time binding to ONE controller. The
// allowlist here is what makes the protocol handler safe: a page can ask
// for any controller it likes and be refused.
type Config struct {
	Controllers []string `json:"controllers"`
	InstalledAt string   `json:"installed_at"`
	Version     string   `json:"version"`
	MachineID   string   `json:"machine_id"`
}

func configPath() string { return filepath.Join(programDataDir(), "bootstrap.json") }

func loadConfig() (Config, error) {
	var config Config
	raw, err := os.ReadFile(configPath())
	if err != nil {
		return config, err
	}
	return config, json.Unmarshal(raw, &config)
}

type statusReport struct {
	Product     string   `json:"product"`
	Version     string   `json:"version"`
	BuildSHA    string   `json:"build_sha"`
	Signed      bool     `json:"signed"`
	Platform    string   `json:"platform"`
	Arch        string   `json:"arch"`
	Installed   bool     `json:"installed"`
	Controllers []string `json:"controllers"`
	MachineID   string   `json:"machine_id,omitempty"`
}

func currentStatus() statusReport {
	report := statusReport{
		Product: ProductName, Version: Version, BuildSHA: BuildSHA,
		Signed: Signed == "true", Platform: runtime.GOOS, Arch: runtime.GOARCH,
	}
	if config, err := loadConfig(); err == nil {
		report.Installed = true
		report.Controllers = config.Controllers
		report.MachineID = config.MachineID
	}
	return report
}

func main() {
	if len(os.Args) < 2 {
		// Double-clicked. On a fresh machine this is the ONLY thing that
		// happens -- there is no helper yet, so no terminalmcp:// handler
		// for the page to hand a session to, and the operator just runs
		// what they downloaded. This used to print usage to stderr and
		// exit 2, which on a double-click is a console window that flashes
		// and vanishes, losing the enrollment they had just created.
		//
		// The controller names the download after that session, so the
		// file itself carries it. Nothing here trusts the name: the
		// pairing is validated, the handle stays opaque, and a name with
		// no pairing falls through to an ordinary interactive install
		// rather than an error.
		os.Exit(runLaunchedDirectly())
	}

	// Windows invokes a protocol handler as `exe "terminalmcp://..."` --
	// no subcommand. Detect that shape before flag parsing.
	if strings.HasPrefix(strings.ToLower(os.Args[1]), proto.Scheme+":") {
		os.Exit(runProtocol(os.Args[1]))
	}

	mode := os.Args[1]
	args := os.Args[2:]
	switch mode {
	case "status":
		encoder := json.NewEncoder(os.Stdout)
		encoder.SetIndent("", "  ")
		_ = encoder.Encode(currentStatus())
		return
	case "version":
		fmt.Printf("%s %s (%s) signed=%v %s/%s\n",
			ProductName, Version, BuildSHA, Signed == "true", runtime.GOOS, runtime.GOARCH)
		return
	case "install":
		set := flag.NewFlagSet("install", flag.ExitOnError)
		controller := set.String("controller", "", "controller origin this helper will be bound to (required)")
		session := set.String("session", "", "optional pending bootstrap session to continue after install")
		_ = set.Parse(args)
		os.Exit(runInstall(*controller, *session))
	case "service":
		os.Exit(runService())
	case "uninstall":
		os.Exit(runUninstall())
	case "protocol":
		set := flag.NewFlagSet("protocol", flag.ExitOnError)
		_ = set.Parse(args)
		if set.NArg() != 1 {
			fmt.Fprintln(os.Stderr, "protocol mode takes exactly one terminalmcp:// URL")
			os.Exit(2)
		}
		os.Exit(runProtocol(set.Arg(0)))
	default:
		fmt.Fprintln(os.Stderr, usage)
		os.Exit(2)
	}
}

const usage = `Terminal MCP Bootstrap

  terminal-mcp-bootstrap install -controller <https://controller> [-session <id>]
  terminal-mcp-bootstrap status
  terminal-mcp-bootstrap version
  terminal-mcp-bootstrap uninstall
  terminal-mcp-bootstrap service          (started by the service manager)
  terminal-mcp-bootstrap "terminalmcp://enroll?handle=...&controller=..."`

// runLaunchedDirectly handles a double-click: recover the pending session
// from this executable's own file name, or fall back to explaining what to
// do next. It never fails loudly for a missing pairing -- a generic
// download is a supported way to arrive here.
func runLaunchedDirectly() int {
	path, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, usage)
		return 2
	}
	pairing, err := proto.ParseFilename(path)
	if err != nil {
		// No pairing, or a name we will not act on. Say what to do rather
		// than printing a flag reference nobody asked for.
		fmt.Println(noPairingMessage)
		return 2
	}
	fmt.Printf("Terminal MCP Bootstrap %s\n", Version)
	fmt.Printf("Controller: %s\n", pairing.Controller)
	// The handle is never printed: it is a credential for its two minutes,
	// and a console window is a screenshot away from a chat message.
	fmt.Println("Continuing the setup session this installer was downloaded for...")
	return runInstall(pairing.Controller, pairing.Handle)
}

const noPairingMessage = `Terminal MCP Bootstrap

This installer was not downloaded for a specific setup session -- or the
browser renamed it on the way down.

Nothing is wrong. Install it, then press "Kết nối máy này" on the Nodes page
again and it will continue from there:

    terminal-mcp-bootstrap install -controller https://<your-controller>`

// runProtocol is the ONLY entry point a web page can reach. It validates
// and forwards; it never executes. A failure here is a refusal, printed
// without echoing the URL back -- hostile input does not belong in a log.
func runProtocol(raw string) int {
	config, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, "Terminal MCP Bootstrap is not installed on this machine.")
		return 3
	}
	request, err := proto.Parse(raw, config.Controllers)
	if err != nil {
		// The reason, never the input.
		fmt.Fprintf(os.Stderr, "refused: %v\n", err)
		return 4
	}
	fields, _ := json.Marshal(request.LogFields())
	fmt.Printf("accepted %s\n", fields)
	return forwardToService(request)
}
