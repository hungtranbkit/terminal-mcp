// terminal-mcp-bootstrap -- the Terminal MCP Bootstrap helper.
//
// # WHAT THIS DOES, AND DELIBERATELY DOES NOT DO
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
	"time"

	"github.com/hungtranbkit/terminal-mcp/helper/internal/proto"
)

// Version is stamped at build time:
//
//	go build -ldflags "-X main.Version=1.0.0 -X main.BuildSHA=$(git rev-parse --short HEAD)"
var (
	Version  = "0.0.0-dev"
	BuildSHA = "unknown"
	// DefaultController is a public HTTPS origin stamped by the official
	// build. It is an address, never a credential; all authority still
	// comes from the owner's authenticated, one-time approval in the UI.
	DefaultController = ""
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

// nodeConfigPath is where windows-setup.ps1 keeps its own node.json
// ($StateDir\node.json). The helper writes the redeemed bootstrap payload
// there so the installer's -Repair path finds credentials already on disk
// instead of trying to enrol a second time with a pairing that has just
// been spent.
func nodeConfigPath() string {
	return filepath.Join(programDataDir(), "node.json")
}

// Config is the helper's install-time binding to ONE controller. The
// allowlist here is what makes the protocol handler safe: a page can ask
// for any controller it likes and be refused.
type Config struct {
	Controllers            []string `json:"controllers"`
	DashboardOrigins       []string `json:"dashboard_origins,omitempty"`
	InstalledAt            string   `json:"installed_at"`
	Version                string   `json:"version"`
	MachineID              string   `json:"machine_id"`
	ApprovedAdminPrincipal string   `json:"approved_admin_principal,omitempty"`
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
	// SCM FIRST, before argument parsing, before the paired-filename
	// lookup, before anything that could touch a pairing. A Win32
	// own-process service that does not complete the handshake makes
	// `sc.exe start` block until SCM's own timeout -- which is exactly the
	// stall that stopped the installer dead at "[3/3]" with no error
	// anywhere. Reaching the dispatcher quickly IS the fix.
	underSCM, detectErr := isWindowsService()
	if decideServiceMode(underSCM, detectErr, os.Args[1:]) == modeSCM {
		os.Exit(runServiceSCM())
	}

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
		// Reached only when SCM detection said no and the operator ran
		// `service` by hand -- decideServiceMode sends the real SCM case
		// to runServiceSCM above.
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

// runLaunchedDirectly is the public one-file path. The filename carries no
// state and may be changed by any browser without changing setup behavior.
func runLaunchedDirectly() int {
	return runGenericSetup(DefaultController)
}

func runGenericSetup(controller string) int {
	// A failed stage offers one real "Repair automatically" action. Retry
	// reruns the same idempotent installer path; Cancel preserves state.
	for {
		code, retry := runGenericSetupOnce(controller)
		if !retry {
			return code
		}
	}
}

func runGenericSetupOnce(controller string) (int, bool) {
	origin, err := proto.NormalizeOrigin(controller)
	if err != nil || !strings.HasPrefix(origin, "https://") {
		return 2, showSetupError("This build has no trusted HTTPS controller. Download the official installer.")
	}
	showSetupStage(1, "Install prerequisites", "Approve Windows UAC. Existing healthy Tailscale, OpenSSH, keys and sessions are preserved.")
	if code := runInstall(origin, ""); code != 0 {
		return code, showSetupError("Prerequisite installation did not finish.")
	}
	showSetupStage(2, "Approve secure connection", "Your browser will show the controller identity and requested permissions. Approve there once; setup never embeds a reusable key.")
	if err := openOwnerApproval(origin + "/dashboard/nodes?join=current&source=windows-setup"); err != nil {
		return 3, showSetupError("The secure approval page could not be opened.")
	}
	if err := waitForInstalledNode(45 * time.Minute); err != nil {
		return 4, showSetupError("Approval or node verification did not finish. No pairing was bypassed.")
	}
	showSetupStage(3, "Verify and finish", "Terminal MCP Node Agent is healthy and ready. Setup completed successfully.")
	return 0, false
}

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
