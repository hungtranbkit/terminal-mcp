package main

import (
	"runtime"
	"strings"
	"testing"
	"time"
)

// The installer used to run with no deadline and its exit code flattened
// to error/no-error. A wedged child could hold the helper open forever,
// and a failure could not say WHICH failure it was.

func skipOnWindows(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("uses POSIX true/false/sleep; the behaviour under test is platform-independent")
	}
}

func TestASuccessfulRunReportsExitZeroAndNoError(t *testing.T) {
	skipOnWindows(t)

	code, err := runBoundedCommand(30*time.Second, newlineFeeder(), "true")

	if code != 0 || err != nil {
		t.Fatalf("want (0, nil), got (%d, %v)", code, err)
	}
}

func TestANonZeroExitIsReturnedAsItsOwnCode(t *testing.T) {
	// windows-setup.ps1 exits 1 for "required steps failed", 2 for
	// "ready with warnings" and 3 for elevation trouble. Which one it was
	// is the whole diagnostic value.
	skipOnWindows(t)

	for _, want := range []int{1, 2, 3} {
		code, err := runBoundedCommand(30*time.Second, newlineFeeder(),
			"sh", "-c", "exit "+string(rune('0'+want)))
		if err == nil {
			t.Fatalf("exit %d: want an error", want)
		}
		if code != want {
			t.Fatalf("want exit code %d, got %d", want, code)
		}
	}
}

func TestAWedgedChildIsCutOffAndReportedAsATimeout(t *testing.T) {
	skipOnWindows(t)
	start := time.Now()

	code, err := runBoundedCommand(300*time.Millisecond, newlineFeeder(), "sleep", "30")

	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("a child past its deadline must be an error")
	}
	if !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("the error must say it timed out: %v", err)
	}
	if code != -1 {
		t.Fatalf("a timeout has no exit code; want -1, got %d", code)
	}
	if elapsed > 5*time.Second {
		t.Fatalf("the deadline did not interrupt the child: %s", elapsed)
	}
}

func TestACommandThatCannotStartIsNotReportedAsAnExitCode(t *testing.T) {
	skipOnWindows(t)

	code, err := runBoundedCommand(5*time.Second, newlineFeeder(), "definitely-not-a-real-binary-xyz")

	if err == nil {
		t.Fatal("want an error")
	}
	if code != -1 {
		t.Fatalf("no process ran, so there is no exit code; want -1, got %d", code)
	}
}

func TestTheChildCanReadStdinWithoutBlocking(t *testing.T) {
	// The real point of the feeder: a child that reads a line gets one
	// immediately instead of hitting EOF and throwing.
	skipOnWindows(t)
	start := time.Now()

	code, err := runBoundedCommand(10*time.Second, newlineFeeder(), "sh", "-c", "read line; exit 0")

	if code != 0 || err != nil {
		t.Fatalf("want (0, nil), got (%d, %v)", code, err)
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("reading stdin blocked -- the feeder is not supplying lines")
	}
}

func TestTheInstallerDeadlineIsGenerousEnoughForARealInstall(t *testing.T) {
	// An ai_coding profile installs OpenSSH, winget packages and npm CLIs
	// on a cold machine. Too short a bound would kill a working install.
	if installerTimeout < 30*time.Minute {
		t.Fatalf("installerTimeout = %s, too short for a real install", installerTimeout)
	}
	if installerTimeout > 2*time.Hour {
		t.Fatalf("installerTimeout = %s, no longer a useful backstop", installerTimeout)
	}
}
