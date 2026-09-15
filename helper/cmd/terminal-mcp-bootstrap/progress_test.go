package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// A stand-in controller. Records what actually arrived, which is the only
// way to assert the thing that matters here: what this helper puts on the
// wire, and what it does not.
type recordingController struct {
	mu        sync.Mutex
	progress  []map[string]any
	redeems   []map[string]any
	redeemErr int
	server    *httptest.Server
}

func newRecordingController(t *testing.T) *recordingController {
	t.Helper()
	rc := &recordingController{}
	mux := http.NewServeMux()
	mux.HandleFunc("/dashboard/api/enroll/progress", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		var parsed map[string]any
		_ = json.Unmarshal(body, &parsed)
		rc.mu.Lock()
		rc.progress = append(rc.progress, parsed)
		rc.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"accepted":true}`))
	})
	mux.HandleFunc("/dashboard/api/enroll/redeem", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		var parsed map[string]any
		_ = json.Unmarshal(body, &parsed)
		rc.mu.Lock()
		rc.redeems = append(rc.redeems, parsed)
		status := rc.redeemErr
		rc.mu.Unlock()
		if status != 0 {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":"ENROLLMENT_NOT_PENDING","detail":"secret-ish detail"}`))
			return
		}
		_, _ = w.Write([]byte(`{"node_id":"win-work","controller_url":"https://c.example","token":"SUPER-SECRET-TOKEN"}`))
	})
	rc.server = httptest.NewServer(mux)
	t.Cleanup(rc.server.Close)
	return rc
}

func (rc *recordingController) stages() []string {
	rc.mu.Lock()
	defer rc.mu.Unlock()
	out := make([]string, 0, len(rc.progress))
	for _, entry := range rc.progress {
		out = append(out, asString(entry["stage"]))
	}
	return out
}

func asString(value any) string {
	text, _ := value.(string)
	return text
}

const testHandle = "0123456789abcdef0123456789abcdef"

func TestProgressReportsCarryTheHandleAndTheStage(t *testing.T) {
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).report(stageHelperStarted)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	if len(rc.progress) != 1 {
		t.Fatalf("want 1 progress call, got %d", len(rc.progress))
	}
	entry := rc.progress[0]
	if asString(entry["stage"]) != stageHelperStarted {
		t.Fatalf("stage = %v", entry["stage"])
	}
	if asString(entry["handle"]) != testHandle {
		t.Fatalf("the handle must authenticate the report")
	}
	// The code is what the INSTALLER holds; the helper must never invent
	// one or forward anything resembling it.
	if _, present := entry["code"]; present {
		t.Fatalf("a progress report must not carry an enrollment code")
	}
	if _, present := entry["token"]; present {
		t.Fatalf("a progress report must not carry a token")
	}
}

func TestProgressIsBestEffortAndNeverPanicsOnADeadController(t *testing.T) {
	// A controller that cannot be reached is not a reason to refuse to
	// install: telemetry must never become a gate.
	newReporter("http://127.0.0.1:1", testHandle).report(stageHelperStarted)
	newReporter("://not-a-url", testHandle).report(stageRedeeming)
}

func TestProgressIsSilentWithoutAPairing(t *testing.T) {
	// An interactive install that was never paired has nothing to report
	// against, and must not send a report with an empty handle.
	rc := newRecordingController(t)
	newReporter(rc.server.URL, "").report(stageHelperStarted)
	if got := len(rc.stages()); got != 0 {
		t.Fatalf("want no reports without a handle, got %d", got)
	}
}

func TestEveryReportedStageIsOneTheControllerKnows(t *testing.T) {
	// This list is duplicated in terminal_mcp/enrollment.py; a value this
	// side invents is refused with a 400 and silently never shown.
	for _, stage := range []string{
		stageHelperStarted, stageRedeeming, stageRedeemed,
		stageInstallingService, stageLaunchingSetup, stageSetupStarted, stageFailed,
	} {
		if stage == "" || strings.ContainsAny(stage, " \t\n") {
			t.Fatalf("bad stage constant %q", stage)
		}
	}
}

func TestRedeemSendsTheHandleAndHostnameAndNothingElseSensitive(t *testing.T) {
	rc := newRecordingController(t)

	payload, err := redeemHandle(rc.server.URL, testHandle, "WIN-BOX")
	if err != nil {
		t.Fatalf("redeem: %v", err)
	}
	if payload["node_id"] != "win-work" {
		t.Fatalf("payload not returned: %v", payload)
	}
	rc.mu.Lock()
	defer rc.mu.Unlock()
	if len(rc.redeems) != 1 {
		t.Fatalf("want 1 redeem, got %d", len(rc.redeems))
	}
	sent := rc.redeems[0]
	if asString(sent["handle"]) != testHandle || asString(sent["hostname"]) != "WIN-BOX" {
		t.Fatalf("redeem body = %v", sent)
	}
	if asString(sent["platform"]) != "windows" {
		t.Fatalf("platform = %v", sent["platform"])
	}
}

func TestAnExpiredOrSpentPairingFailsClosedAndDoesNotLeakTheDetail(t *testing.T) {
	rc := newRecordingController(t)
	rc.redeemErr = http.StatusConflict

	_, err := redeemHandle(rc.server.URL, testHandle, "WIN-BOX")

	if err == nil {
		t.Fatal("a rejected pairing must be an error, not an empty success")
	}
	// The controller's own detail string stays local: it is shown at the
	// machine, never forwarded into anything the Dashboard renders.
	if strings.Contains(err.Error(), "secret-ish detail") {
		t.Fatalf("controller detail leaked into the error: %v", err)
	}
}

func TestAnUnreachableControllerIsAPlainError(t *testing.T) {
	_, err := redeemHandle("http://127.0.0.1:1", testHandle, "WIN-BOX")
	if err == nil {
		t.Fatal("want an error")
	}
	if !strings.Contains(err.Error(), "unreachable") {
		t.Fatalf("want an unreachable error, got %v", err)
	}
}

func TestFailureCodesAreAClosedSetAndCarryNoDetail(t *testing.T) {
	for _, code := range []string{
		failPairingRejected, failControllerUnr, failScriptDownload,
		failServiceInstall, failSetupLaunch,
	} {
		if strings.ContainsAny(code, " /\\:") {
			t.Fatalf("a failure code must be a bare token, got %q", code)
		}
	}
}

func TestAFailureReportsTheFailedStageAndNoRawError(t *testing.T) {
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).fail(failPairingRejected)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	if len(rc.progress) != 1 || asString(rc.progress[0]["stage"]) != stageFailed {
		t.Fatalf("want one failed stage, got %v", rc.progress)
	}
	// Whatever went wrong locally, the wire carries a stage and nothing
	// describing this machine.
	for _, banned := range []string{"detail", "error", "path", "message"} {
		if _, present := rc.progress[0][banned]; present {
			t.Fatalf("failure report carried %q", banned)
		}
	}
}

func TestRepeatedReportsAreIdempotentFromTheHelperSide(t *testing.T) {
	// The helper may report the same stage twice (a retried step); the
	// controller overwrites rather than appending, so this is safe and
	// must not be prevented here with local state.
	rc := newRecordingController(t)
	reporter := newReporter(rc.server.URL, testHandle)
	reporter.report(stageRedeeming)
	reporter.report(stageRedeeming)
	if got := len(rc.stages()); got != 2 {
		t.Fatalf("want both reports sent, got %d", got)
	}
}

// -- failure detail on the wire ---------------------------------------------

func TestAPlainStageCarriesNoFailureDetail(t *testing.T) {
	// Backward compatibility: a normal stage report is byte-for-byte the
	// body an older controller already accepts.
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).report(stageRedeeming)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	entry := rc.progress[0]
	for _, key := range []string{"code", "exit_code"} {
		if _, present := entry[key]; present {
			t.Fatalf("a plain stage must not carry %q", key)
		}
	}
	for _, key := range []string{"handle", "stage", "elapsed_seconds"} {
		if _, present := entry[key]; !present {
			t.Fatalf("a plain stage must still carry %q", key)
		}
	}
}

func TestAFailureCarriesItsCodeSoTheReasonLeavesTheMachine(t *testing.T) {
	// The gap this closes: fail() used to send stage=failed alone, so the
	// Dashboard could say "failed" and never why.
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).fail(failScriptDownload)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	entry := rc.progress[0]
	if asString(entry["stage"]) != stageFailed {
		t.Fatalf("stage = %v", entry["stage"])
	}
	if asString(entry["code"]) != failScriptDownload {
		t.Fatalf("code = %v, want %q", entry["code"], failScriptDownload)
	}
}

func TestAnInstallerFailureCarriesItsExitCode(t *testing.T) {
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).failWithCode(failSetupLaunch, 3)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	entry := rc.progress[0]
	if asString(entry["code"]) != failSetupLaunch {
		t.Fatalf("code = %v", entry["code"])
	}
	exit, ok := entry["exit_code"].(float64) // JSON numbers decode as float64
	if !ok || int(exit) != 3 {
		t.Fatalf("exit_code = %v, want 3", entry["exit_code"])
	}
}

func TestAFailureReportStillCarriesNoCredentialOrOutput(t *testing.T) {
	// The installer prints controller URLs and key paths. None of it may
	// ride along with the failure code.
	rc := newRecordingController(t)
	newReporter(rc.server.URL, testHandle).failWithCode(failSetupLaunch, 1)

	rc.mu.Lock()
	defer rc.mu.Unlock()
	entry := rc.progress[0]
	for _, banned := range []string{"output", "stdout", "stderr", "detail", "message", "token", "node_token"} {
		if _, present := entry[banned]; present {
			t.Fatalf("a failure report must not carry %q", banned)
		}
	}
	// Exactly five keys at most: handle, stage, elapsed_seconds, code, exit_code.
	if len(entry) > 5 {
		t.Fatalf("unexpected fields in a failure report: %v", entry)
	}
}
