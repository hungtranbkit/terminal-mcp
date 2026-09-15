package main

import (
	"errors"
	"go/parser"
	"go/printer"
	"go/token"
	"strings"
	"testing"
)

// The stall these encode: sc.exe create registered this binary as a Win32
// own-process service, the binary never completed the SCM handshake, SCM
// waited for a SERVICE_RUNNING that could not arrive, and `sc.exe start`
// blocked for its whole timeout. The installer showed "[3/3] install and
// start the service" and nothing else -- and because every progress
// callback lives in continueSession, which runs only AFTER step 3 returns,
// the controller saw nothing at all.

func TestSCMStartedProcessEntersServiceMode(t *testing.T) {
	if got := decideServiceMode(true, nil, nil); got != modeSCM {
		t.Fatalf("SCM-started process must enter service mode, got %v", got)
	}
}

func TestAnInteractiveProcessNeverEntersServiceMode(t *testing.T) {
	// A double-click must reach the paired-filename path, not the SCM
	// dispatcher -- which on a non-service process blocks and then fails.
	for _, args := range [][]string{nil, {}, {"status"}, {"install", "-controller", "https://c"}} {
		if got := decideServiceMode(false, nil, args); got != modeCLI {
			t.Fatalf("args %v must stay in CLI mode, got %v", args, got)
		}
	}
}

func TestTheExplicitServiceArgumentStillForcesServiceMode(t *testing.T) {
	// This is how sc.exe is configured to invoke us (binPath "...exe"
	// service), so it must work even where detection is unavailable.
	if got := decideServiceMode(false, nil, []string{"service"}); got != modeSCM {
		t.Fatalf("explicit service argument must force SCM mode, got %v", got)
	}
	if got := decideServiceMode(false, errors.New("nope"), []string{"service"}); got != modeSCM {
		t.Fatalf("explicit service argument must win over a detection error, got %v", got)
	}
}

func TestADetectionFailureFallsBackToCLINotService(t *testing.T) {
	// Guessing "service" for a double-click would leave the operator
	// staring at a process reporting to an SCM that is not there: the same
	// silent hang in a new costume.
	if got := decideServiceMode(true, errors.New("cannot determine"), nil); got != modeCLI {
		t.Fatalf("a detection error must fall back to CLI, got %v", got)
	}
}

func TestServiceModeDecisionIgnoresAProtocolURL(t *testing.T) {
	// terminalmcp:// arrives as argv[1]; it is emphatically not a service
	// start and must not be treated as one.
	args := []string{"terminalmcp://enroll?handle=REDACTED&controller=https://c"}
	if got := decideServiceMode(false, nil, args); got != modeCLI {
		t.Fatalf("a protocol URL must stay in CLI mode, got %v", got)
	}
}

func TestAlreadyUsableServiceStatesAreNotRestarted(t *testing.T) {
	// Asking SCM to start a RUNNING service is how a Repair or a second
	// enrollment on the same machine used to fail for no reason.
	for _, state := range []string{"RUNNING", "START_PENDING", "CONTINUE_PENDING"} {
		if !serviceStateIsUsable(state) {
			t.Fatalf("%s must count as already started", state)
		}
	}
	for _, state := range []string{"STOPPED", "STOP_PENDING", "PAUSED", "", "GARBAGE"} {
		if serviceStateIsUsable(state) {
			t.Fatalf("%s must NOT count as already started", state)
		}
	}
}

func TestServiceModeNeverTouchesAPairing(t *testing.T) {
	// SCM starts this process on every boot. A service start that redeemed
	// the pending pairing would silently burn a single-use credential the
	// operator was about to use -- a worse bug than the stall this change
	// fixes. Asserted structurally because the real dispatcher cannot run
	// in a unit test: the SCM entry point must not reach the pairing code.
	code := goCodeWithoutComments(t, "svc_windows.go")
	for _, forbidden := range []string{
		"continueSession", "redeemHandle", "ParseFilename",
		"runInstall", "runLaunchedDirectly", "downloadSetupScript",
	} {
		if strings.Contains(code, forbidden) {
			t.Fatalf("the SCM entry point must not reference %s", forbidden)
		}
	}
	// And it must run the pipe loop, which is the only thing it is for.
	if !strings.Contains(code, "servePipeUntil") {
		t.Fatal("the SCM entry point must serve the pipe")
	}
}

func TestTheServiceAcceptsOnlyStopShutdownAndInterrogate(t *testing.T) {
	// No Pause/Continue and no service-specific control codes: the IPC
	// surface must not widen just because the handshake was added.
	text := goCodeWithoutComments(t, "svc_windows.go")
	if !strings.Contains(text, "svc.AcceptStop|svc.AcceptShutdown") &&
		!strings.Contains(text, "svc.AcceptStop | svc.AcceptShutdown") {
		t.Fatal("the accepted-command set must be exactly Stop|Shutdown")
	}
	for _, forbidden := range []string{"AcceptPauseAndContinue", "AcceptParamChange", "AcceptPreShutdown"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("the service must not accept %s", forbidden)
		}
	}
}

func TestTheServiceNameIsUnchanged(t *testing.T) {
	// sc.exe create, sc.exe query, the installed service and any existing
	// machine all agree on this string; changing it would orphan services
	// already installed in the field.
	if ServiceName != "TerminalMCPBootstrap" {
		t.Fatalf("ServiceName = %q, must stay TerminalMCPBootstrap", ServiceName)
	}
}

// goCodeWithoutComments returns a file's Go source with comments stripped,
// so a structural assertion tests the CODE and not the prose explaining it.
// (The first version of these tests failed on the word "continueSession"
// appearing in a comment that says the service must never call it.)
func goCodeWithoutComments(t *testing.T, name string) string {
	t.Helper()
	fileSet := token.NewFileSet()
	parsed, err := parser.ParseFile(fileSet, name, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", name, err)
	}
	var out strings.Builder
	if err := printer.Fprint(&out, fileSet, parsed); err != nil {
		t.Fatalf("print %s: %v", name, err)
	}
	return out.String()
}
