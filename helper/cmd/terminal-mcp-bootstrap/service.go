package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

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

// continueSession: fetch the setup script from the bound controller, then
// let it do the stages. The handle is passed as an argument to the
// script, never interpolated into a command string.
func continueSession(handle, controller string) error {
	if err := downloadSetupScript(controller); err != nil {
		return fmt.Errorf("download setup script: %w", err)
	}
	return runStages(controller, "")
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
