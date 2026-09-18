package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/hungtranbkit/terminal-mcp/helper/internal/proto"
)

// Typed actions, a closed set, mirroring the protocol grammar. The pipe
// carries these -- never a command string. A privileged service that
// accepted arbitrary RPC would undo every check in internal/proto.
type pipeRequest struct {
	Action     string `json:"action"`
	Handle     string `json:"handle,omitempty"`
	Session    string `json:"session,omitempty"`
	Controller string `json:"controller"`
}

type pipeResponse struct {
	OK     bool           `json:"ok"`
	Error  string         `json:"error,omitempty"`
	Status *statusReport  `json:"status,omitempty"`
}

var pipeActions = map[string]bool{
	"GetStatus": true, "Bootstrap": true, "Repair": true,
	"Reconnect": true, "UpdateProfile": true, "GetProgress": true,
}

// forwardToService hands a VALIDATED request to the privileged service.
// The protocol process does no work of its own -- that separation is why
// a web page cannot reach anything that executes.
func forwardToService(request proto.Request) int {
	payload := pipeRequest{Controller: request.Controller}
	switch request.Action {
	case proto.ActionEnroll:
		payload.Action, payload.Handle = "Bootstrap", request.Handle
	case proto.ActionRepair:
		payload.Action = "Repair"
	case proto.ActionStatus:
		payload.Action = "GetStatus"
	default:
		fmt.Fprintln(os.Stderr, "refused: unknown action")
		return 4
	}
	response, err := sendToPipe(payload)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot reach the Terminal MCP Bootstrap service: %v\n", err)
		return 5
	}
	if !response.OK {
		fmt.Fprintf(os.Stderr, "service refused: %s\n", response.Error)
		return 6
	}
	return 0
}

// handlePipeRequest is the service side. It re-validates everything --
// the caller is local, but "local" is not "trusted", and a second check
// costs nothing next to what it guards.
func handlePipeRequest(raw []byte, config Config) pipeResponse {
	var request pipeRequest
	if err := json.Unmarshal(raw, &request); err != nil {
		return pipeResponse{Error: "malformed request"}
	}
	if !pipeActions[request.Action] {
		return pipeResponse{Error: "unknown action"}
	}
	origin, err := proto.NormalizeOrigin(request.Controller)
	if err != nil {
		return pipeResponse{Error: "bad controller"}
	}
	allowed := false
	for _, entry := range config.Controllers {
		if normalized, err := proto.NormalizeOrigin(entry); err == nil && normalized == origin {
			allowed = true
			break
		}
	}
	if !allowed {
		return pipeResponse{Error: "controller not in allowlist"}
	}
	if request.Action == "Bootstrap" && !handleLooksValid(request.Handle) {
		return pipeResponse{Error: "bad handle"}
	}

	switch request.Action {
	case "GetStatus":
		status := currentStatus()
		return pipeResponse{OK: true, Status: &status}
	case "Bootstrap":
		if err := continueSession(request.Handle, origin); err != nil {
			return pipeResponse{Error: err.Error()}
		}
		return pipeResponse{OK: true}
	case "Repair":
		if err := runStages(origin, "-Repair"); err != nil {
			return pipeResponse{Error: err.Error()}
		}
		return pipeResponse{OK: true}
	}
	return pipeResponse{Error: "not implemented in this build"}
}

func handleLooksValid(handle string) bool {
	if len(handle) != 32 {
		return false
	}
	for _, character := range strings.ToLower(handle) {
		if !strings.ContainsRune("0123456789abcdef", character) {
			return false
		}
	}
	return true
}

// continueSession: redeem the pairing, save what it returns, then let the
// installer do the stages -- narrating each step to the controller as it
// is entered.
//
// The previous version accepted a handle, said in a comment that it passed
// it to the script, and then called runStages(controller, "") -- dropping
// it. The consequence in production was exact and silent: the controller
// never saw a redeem, the enrollment stayed pending until it expired, and
// the Dashboard had nothing to show because nothing had been reported.
// Redeeming here is what makes the paired web flow able to finish at all.
//
// The bootstrap payload is written to the node config with the same
// atomic, 0600 discipline as every other credential this helper touches,
// and the installer is then run in -Repair mode, which is its documented
// "credentials are already on disk" path. Nothing from the payload is
// passed on a command line, where it would be visible to any process
// listing on the machine.
// installerTimeout bounds the whole install. Generous on purpose: an
// ai_coding profile installs OpenSSH, winget packages and npm CLIs, which
// legitimately takes many minutes on a cold machine. This is a backstop
// against a wedged child, not a performance budget.
const installerTimeout = 45 * time.Minute

func continueSession(handle, controller string) error {
	report := newReporter(controller, handle)
	report.report(stageHelperStarted)

	// Repair what an older helper build may have left here: a node.json
	// with the token embedded and no node.token beside it. Done before
	// anything else so a machine that already has the bad pair is fixed
	// even on a run that later fails for an unrelated reason.
	if migrated, err := sanitizeExistingNodeConfig(); err != nil {
		fmt.Fprintf(os.Stderr, "      ! could not sanitise the existing node config: %v\n", err)
	} else if migrated {
		fmt.Println("recovered the node token from an older node.json and tightened it")
	}

	report.report(stageRedeeming)
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "windows"
	}
	payload, err := redeemHandle(controller, handle, hostname)
	if err != nil {
		report.fail(failPairingRejected)
		return fmt.Errorf("redeem pairing: %w", err)
	}
	report.report(stageRedeemed)

	// Split the response into exactly what the installer's own first run
	// leaves on disk. Writing the raw payload here is what broke -Repair:
	// it gates on node.token existing, and the old code never wrote one.
	config, token, err := canonicalNodeConfig(payload, controller)
	if err != nil {
		report.fail(failServiceInstall)
		return fmt.Errorf("bootstrap payload: %w", err)
	}
	report.report(stageInstallingService)
	if err := persistNodeConfig(config, token); err != nil {
		report.fail(failServiceInstall)
		return fmt.Errorf("persist node config: %w", err)
	}

	if err := downloadSetupScript(controller); err != nil {
		report.fail(failScriptDownload)
		return fmt.Errorf("download setup script: %w", err)
	}

	// -Repair, deliberately: this helper has ALREADY consumed the
	// enrollment and owns the issued config, so the installer must reuse
	// what is on disk rather than try to enrol a second time with a
	// pairing that is now spent.
	report.report(stageLaunchingSetup)
	code, err := runStagesBounded(controller, "-Repair", installerTimeout)
	if err != nil {
		report.failWithCode(failSetupLaunch, code)
		return fmt.Errorf("run setup: %w", err)
	}
	// The installer has taken over and reports its own stages from here;
	// this is the last thing the helper itself says.
	report.report(stageSetupStarted)
	return nil
}

func runService() int {
	config, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, "service: not installed (no config)")
		return 3
	}
	return serveePipe(config)
}

func runUninstall() int {
	fmt.Println("[1/3] removing the service")
	if err := removeService(); err != nil {
		fmt.Fprintf(os.Stderr, "      ! %v\n", err)
	}
	fmt.Println("[2/3] removing the terminalmcp:// registration")
	if err := unregisterProtocol(); err != nil {
		fmt.Fprintf(os.Stderr, "      ! %v\n", err)
	}
	fmt.Println("[3/3] removing helper state")
	// Only what this helper owns. Never OpenSSH, Tailscale, Git, Node,
	// Python or anything the machine's owner installed.
	_ = os.Remove(configPath())
	_ = os.RemoveAll(programFilesDir())
	fmt.Println("Terminal MCP Bootstrap removed. OpenSSH, Tailscale and developer tools were left installed.")
	return 0
}
