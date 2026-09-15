package main

import (
	"runtime"
	"strings"
	"testing"
	"time"
)

// runCommand had no deadline at all. `sc.exe start` blocks while SCM waits
// for a SERVICE_RUNNING the old binary could never report, so "no timeout"
// meant the installer could wait forever with "[3/3]" on screen.

func TestACommandThatHangsIsCutOffRatherThanWaitedOnForever(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX sleep; the behaviour under test is platform-independent")
	}
	start := time.Now()

	err := runCommandTimeout(300*time.Millisecond, "sleep", "30")

	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("a command past its deadline must be an error, not a silent success")
	}
	if !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("the error must say it timed out, got: %v", err)
	}
	// The point is that it RETURNS. A generous bound: the assertion is
	// "seconds, not forever", not a benchmark.
	if elapsed > 5*time.Second {
		t.Fatalf("timeout did not actually interrupt the command: %s", elapsed)
	}
}

func TestAQuickCommandIsNotPenalisedByTheTimeout(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX true(1)")
	}
	if err := runCommandTimeout(30*time.Second, "true"); err != nil {
		t.Fatalf("a successful command must not error: %v", err)
	}
}

func TestAFailingCommandStillReportsItsOwnError(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX false(1)")
	}
	err := runCommandTimeout(30*time.Second, "false")
	if err == nil {
		t.Fatal("want an error")
	}
	if strings.Contains(err.Error(), "timed out") {
		t.Fatalf("a plain failure must not be reported as a timeout: %v", err)
	}
}

func TestTheServiceStartDeadlineMatchesSCMsOwnTimeout(t *testing.T) {
	// ServicesPipeTimeout defaults to 30s. Shorter would cut off a healthy
	// service that is merely slow to start; longer reintroduces the stall.
	if defaultCommandTimeout != 30*time.Second {
		t.Fatalf("defaultCommandTimeout = %s, want 30s", defaultCommandTimeout)
	}
}
