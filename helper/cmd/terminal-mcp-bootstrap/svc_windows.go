//go:build windows

package main

// The Windows Service Control Manager handshake.
//
// Without this the service registered by `sc.exe create` was a process SCM
// started and then waited on forever: a Win32 own-process service must call
// StartServiceCtrlDispatcher and report SERVICE_RUNNING, and this binary
// did neither. SCM's own timeout (ServicesPipeTimeout, 30s by default) was
// the only thing that eventually ended the wait, and `sc.exe start` blocked
// for all of it while the installer showed "[3/3]" and nothing else.
//
// What runs inside the service is deliberately the SAME named-pipe loop as
// before. This adds the handshake and a way to stop; it does not widen what
// the service can be asked to do, and it must never redeem a pairing --
// SCM starts this process at boot, and a boot that silently consumed a
// pending enrollment would be a far worse bug than the one being fixed.

import (
	"fmt"
	"os"
	"time"

	"golang.org/x/sys/windows/svc"
)

// acceptedCommands is the closed set this service answers. Stop and
// Shutdown so it can be ended cleanly; Interrogate because SCM uses it to
// poll state and a service that ignores it looks wedged. Nothing else --
// no Pause/Continue, no service-specific control codes, so there is no
// extra surface for anything on the machine to poke at.
const acceptedCommands = svc.AcceptStop | svc.AcceptShutdown

// bootstrapService is the SCM side of the helper. It owns state reporting
// only; the work is the same pipe loop the service has always run.
type bootstrapService struct {
	// serve runs the long-lived work and returns when stop is closed.
	// Injected so the state machine can be exercised without a real pipe.
	serve func(stop <-chan struct{}) int
}

// Execute is the SCM contract: report StartPending, get the work running,
// report Running, then answer control requests until asked to stop.
func (s *bootstrapService) Execute(args []string, requests <-chan svc.ChangeRequest,
	status chan<- svc.Status) (bool, uint32) {
	// StartPending first and immediately. Every second between process
	// start and this line is a second sc.exe start spends blocked.
	status <- svc.Status{State: svc.StartPending}

	stop := make(chan struct{})
	done := make(chan int, 1)
	go func() { done <- s.serve(stop) }()

	// Running is what SCM is waiting for; sc.exe start returns here.
	status <- svc.Status{State: svc.Running, Accepts: acceptedCommands}

	for {
		select {
		case code := <-done:
			// The work ended on its own -- a pipe that could not be
			// opened, say. Report stopped rather than sitting in Running
			// with nothing behind it.
			status <- svc.Status{State: svc.StopPending}
			return false, uint32(code)
		case request := <-requests:
			switch request.Cmd {
			case svc.Interrogate:
				status <- request.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				close(stop)
				// Bounded: a pipe accept that will not unblock must not
				// hold the machine's shutdown open.
				select {
				case code := <-done:
					return false, uint32(code)
				case <-time.After(10 * time.Second):
					return false, 0
				}
			default:
				// Unexpected control code. Ignored deliberately: this
				// service accepts Stop, Shutdown and Interrogate, and
				// answering anything else would be inventing a contract.
			}
		}
	}
}

// runServiceSCM is the entry point taken when the Service Control Manager
// started us. It NEVER looks at a pairing: the paired-filename path and
// continueSession belong to the interactive run, not to a service that
// Windows starts again on every boot.
func runServiceSCM() int {
	config, err := loadConfig()
	if err != nil {
		// Not installed. Returning non-zero lets SCM record a real failure
		// instead of a service that reports Running with nothing to do.
		fmt.Fprintln(os.Stderr, "service: not installed (no config)")
		return 3
	}
	handler := &bootstrapService{
		serve: func(stop <-chan struct{}) int { return servePipeUntil(config, stop) },
	}
	if err := svc.Run(ServiceName, handler); err != nil {
		fmt.Fprintf(os.Stderr, "service: SCM dispatch failed: %v\n", err)
		return 1
	}
	return 0
}

// isWindowsService asks the platform whether SCM started us.
func isWindowsService() (bool, error) { return svc.IsWindowsService() }
