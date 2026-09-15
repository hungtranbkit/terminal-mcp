package main

// Progress reporting, and the handle exchange it narrates.
//
// WHY THIS FILE EXISTS. Until now the helper told the controller nothing
// between the operator double-clicking the downloaded binary and the
// PowerShell installer reaching its own first stage. Every way the helper
// can fail before that point -- SmartScreen killing it, a pairing that
// expired while the operator clicked through UAC, a controller it cannot
// reach -- produced total silence on the Dashboard, which is
// indistinguishable from "nothing was ever clicked".
//
// So each step reports as it is ENTERED, and a failure reports a stage of
// its own with a short machine-readable code. The controller answers 202
// to everything, so none of this can block or fail the install: progress
// is telemetry, never a gate.
//
// WHAT IS NEVER SENT. The pairing handle authenticates these calls and is
// never logged by either side. The bootstrap payload -- node token, SSH
// material, anything the redeem returns -- is never included in a progress
// report, and errors are reduced to a fixed vocabulary of codes rather
// than forwarding a raw error string that could carry a URL, a path or a
// credential.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

// The stages this helper reports. They match terminal_mcp/enrollment.py's
// STAGES exactly; a value this side invents would be refused with a 400
// and simply not shown, so the two lists are kept in step deliberately.
const (
	stageHelperStarted     = "helper_started"
	stageRedeeming         = "redeeming"
	stageRedeemed          = "redeemed"
	stageInstallingService = "installing_service"
	stageLaunchingSetup    = "launching_setup"
	stageSetupStarted      = "setup_started"
	stageFailed            = "failed"
)

// Failure codes. Deliberately a closed set: the operator sees a sentence
// the Dashboard writes, not an error string from this process, so nothing
// from the machine's filesystem or network configuration can travel in a
// failure report.
const (
	failPairingRejected = "pairing_rejected"
	failControllerUnr   = "controller_unreachable"
	failScriptDownload  = "script_download_failed"
	failServiceInstall  = "service_install_failed"
	failSetupLaunch     = "setup_launch_failed"
)

var progressStart = time.Now()

// reporter carries what every progress call needs. A zero handle disables
// reporting entirely, which is the honest state for an interactive install
// that was never paired with a pending enrollment.
type reporter struct {
	controller string
	handle     string
	client     *http.Client
}

func newReporter(controller, handle string) *reporter {
	return &reporter{controller: controller, handle: handle,
		client: &http.Client{Timeout: 10 * time.Second}}
}

// report is best-effort by contract. It never returns an error and never
// blocks the install for more than its own short timeout: a controller
// that cannot be reached for telemetry is not a reason to refuse to
// install, and the Dashboard's stall warning already covers the silence.
func (r *reporter) report(stage string) {
	if r == nil || r.handle == "" || r.controller == "" {
		return
	}
	body, err := json.Marshal(map[string]any{
		"handle":          r.handle,
		"stage":           stage,
		"elapsed_seconds": int(time.Since(progressStart).Seconds()),
	})
	if err != nil {
		return
	}
	request, err := http.NewRequest(http.MethodPost,
		r.controller+"/dashboard/api/enroll/progress", bytes.NewReader(body))
	if err != nil {
		return
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := r.client.Do(request)
	if err != nil {
		return
	}
	// Drain and close so the connection can be reused; the body carries
	// {"accepted": true} and nothing this side needs.
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<12))
	_ = response.Body.Close()
}

// fail reports the failed stage. The code is printed locally for whoever
// is standing at the machine, and sent as the stage's companion so the
// Dashboard can say something specific -- but the code is chosen from the
// closed set above, never built from an error value.
func (r *reporter) fail(code string) {
	fmt.Fprintf(os.Stderr, "terminal-mcp-bootstrap: failed (%s)\n", code)
	r.report(stageFailed)
}

// redeemHandle exchanges the one-time pairing handle for the bootstrap
// payload.
//
// This call did not exist before. continueSession accepted a handle,
// documented that it passed it to the installer, and then dropped it --
// so the paired web flow could never complete: the controller was never
// told the machine had arrived, and the installer ran with no credentials
// of its own. Redemption is single-use and the CONTROLLER consumes the
// enrollment, so nothing this process is left holding can be replayed.
func redeemHandle(controller, handle, hostname string) (map[string]any, error) {
	body, err := json.Marshal(map[string]any{
		"handle":   handle,
		"hostname": hostname,
		"platform": "windows",
	})
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 60 * time.Second}
	request, err := http.NewRequest(http.MethodPost,
		controller+"/dashboard/api/enroll/redeem", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("controller unreachable")
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("controller unreachable")
	}
	if response.StatusCode != http.StatusOK {
		// The controller's own detail is deliberately NOT forwarded into
		// a progress report; it is shown locally only. An expired or
		// already-spent pairing lands here, which is the single most
		// likely first-run failure.
		return nil, fmt.Errorf("pairing rejected (HTTP %d)", response.StatusCode)
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("controller returned an unreadable payload")
	}
	return payload, nil
}
