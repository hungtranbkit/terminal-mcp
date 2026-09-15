package main

// Deciding whether this process is a Windows service, kept separate from
// the code that can only run on Windows.
//
// WHY THIS IS ITS OWN FILE. The bug it fixes was invisible on every machine
// a developer runs: `sc.exe create` registered this binary as a Win32
// own-process service, but the binary never performed the SCM handshake, so
// the Service Control Manager waited for a SERVICE_RUNNING that could never
// arrive. `sc.exe start` blocked, runCommand had no timeout, and the
// installer stopped dead at "[3/3] install and start the service" -- with
// every progress callback sitting in continueSession, which runs only after
// step 3 returns. The controller therefore saw nothing at all.
//
// The decision itself is pure, so it is unit-tested on any platform; only
// the dispatch it gates is Windows-only.

// serviceModeDecision is what main() does with the process it finds itself
// in. Three outcomes, deliberately explicit rather than a bool: "run the
// SCM handler", "run the CLI", and "run the CLI even though SCM detection
// failed", which is the safe fallback and must not be mistaken for a
// confident answer.
type serviceModeDecision int

const (
	// modeCLI: a human or the protocol handler started this. Normal
	// argument parsing applies.
	modeCLI serviceModeDecision = iota
	// modeSCM: the Service Control Manager started this, so the FIRST
	// thing that must happen is the handshake -- before flag parsing,
	// before the paired-filename lookup, and before anything that could
	// touch a pairing.
	modeSCM
)

// decideServiceMode maps what we can observe to what to do.
//
// `underSCM` is the platform's own answer (svc.IsWindowsService on
// Windows, always false elsewhere) and `detectErr` records that the
// question could not be answered. An explicit `service` argument still
// forces service mode, because that is how sc.exe is configured to invoke
// us and it must keep working even if detection is unavailable.
//
// The fallback when detection fails is modeCLI, NOT modeSCM: guessing
// "service" for an interactive double-click would leave the operator
// staring at a process that reports to an SCM that is not there, which is
// the same silent hang in a new costume.
func decideServiceMode(underSCM bool, detectErr error, args []string) serviceModeDecision {
	if len(args) > 0 && args[0] == "service" {
		return modeSCM
	}
	if detectErr != nil {
		return modeCLI
	}
	if underSCM {
		return modeSCM
	}
	return modeCLI
}

// serviceStartStates are the SCM states in which "start the service" has
// already been achieved. Queried before starting so a re-run, a Repair, or
// a second enrollment on the same machine does not fail on a service that
// is simply already there -- and never creates a duplicate.
func serviceStateIsUsable(state string) bool {
	switch state {
	case "RUNNING", "START_PENDING", "CONTINUE_PENDING":
		return true
	}
	return false
}
