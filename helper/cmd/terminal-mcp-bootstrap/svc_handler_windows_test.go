//go:build windows

package main

import (
	"testing"
	"time"

	"golang.org/x/sys/windows/svc"
)

// The SCM state machine, exercised without a real Service Control Manager:
// Execute is driven directly with the same channels svc.Run would supply.
// This is the part that was missing entirely, so it is worth pinning even
// though the dispatcher itself cannot run here.

func drive(t *testing.T, handler *bootstrapService) (chan<- svc.ChangeRequest, <-chan svc.Status, chan struct{}) {
	t.Helper()
	requests := make(chan svc.ChangeRequest, 4)
	status := make(chan svc.Status, 16)
	done := make(chan struct{})
	go func() {
		handler.Execute(nil, requests, status)
		close(done)
	}()
	return requests, status, done
}

func waitState(t *testing.T, status <-chan svc.Status, want svc.State) svc.Status {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case got := <-status:
			if got.State == want {
				return got
			}
		case <-deadline:
			t.Fatalf("never reached state %v", want)
		}
	}
}

func TestTheServiceReportsStartPendingThenRunning(t *testing.T) {
	// Reaching Running is literally what unblocks `sc.exe start`.
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int { <-stop; return 0 }}
	requests, status, done := drive(t, handler)

	waitState(t, status, svc.StartPending)
	running := waitState(t, status, svc.Running)

	if running.Accepts&svc.AcceptStop == 0 || running.Accepts&svc.AcceptShutdown == 0 {
		t.Fatalf("a running service must accept Stop and Shutdown, got %v", running.Accepts)
	}
	requests <- svc.ChangeRequest{Cmd: svc.Stop}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Stop did not end Execute")
	}
}

func TestStopEndsTheWorkAndTheService(t *testing.T) {
	stopped := make(chan struct{})
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int {
		<-stop
		close(stopped)
		return 0
	}}
	requests, status, done := drive(t, handler)
	waitState(t, status, svc.Running)

	requests <- svc.ChangeRequest{Cmd: svc.Stop}

	select {
	case <-stopped:
	case <-time.After(3 * time.Second):
		t.Fatal("the work was never told to stop")
	}
	<-done
}

func TestShutdownIsTreatedLikeStop(t *testing.T) {
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int { <-stop; return 0 }}
	requests, status, done := drive(t, handler)
	waitState(t, status, svc.Running)

	requests <- svc.ChangeRequest{Cmd: svc.Shutdown}

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Shutdown must end the service")
	}
}

func TestInterrogateIsAnsweredAndDoesNotStopTheService(t *testing.T) {
	// SCM polls with Interrogate; a service that ignores it looks wedged.
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int { <-stop; return 0 }}
	requests, status, done := drive(t, handler)
	current := waitState(t, status, svc.Running)

	requests <- svc.ChangeRequest{Cmd: svc.Interrogate, CurrentStatus: current}

	select {
	case got := <-status:
		if got.State != svc.Running {
			t.Fatalf("Interrogate must echo Running, got %v", got.State)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Interrogate was not answered")
	}
	select {
	case <-done:
		t.Fatal("Interrogate must not stop the service")
	case <-time.After(200 * time.Millisecond):
	}
	requests <- svc.ChangeRequest{Cmd: svc.Stop}
	<-done
}

func TestWorkEndingOnItsOwnStopsTheServiceRatherThanReportingRunningForever(t *testing.T) {
	// A pipe that cannot be opened used to leave the process alive with
	// nothing behind it.
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int { return 1 }}
	_, status, done := drive(t, handler)

	waitState(t, status, svc.StopPending)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Execute must return when the work ends")
	}
}

func TestAStuckWorkerCannotHoldWindowsShutdownOpenForever(t *testing.T) {
	handler := &bootstrapService{serve: func(stop <-chan struct{}) int {
		select {} // never returns, like an Accept that will not unblock
	}}
	requests, status, done := drive(t, handler)
	waitState(t, status, svc.Running)

	requests <- svc.ChangeRequest{Cmd: svc.Stop}

	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("Stop must be bounded even when the worker will not end")
	}
}
