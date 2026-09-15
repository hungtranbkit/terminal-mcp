package main

// Running a child process with a deadline, a readable stdin, and its exit
// code preserved.
//
// Split out of the Windows-only installer launcher so the part that has
// nothing to do with Windows -- deadline handling, exit-code extraction,
// stdin wiring -- is exercised on any platform. The installer path itself
// still needs a real powershell.exe and a real machine.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"time"
)

// runBoundedCommand runs a command, feeding it `stdin`, and returns its
// EXIT CODE alongside any error.
//
// The exit code is the point: flattening it to error/no-error is what left
// the helper unable to say WHICH installer failure happened. -1 means "no
// exit code exists" -- the deadline fired, or the process could not be
// started at all.
//
// Output is passed through to this process's console and deliberately NOT
// captured: the installer prints controller URLs and key paths, and none
// of that belongs in something the helper might upload.
func runBoundedCommand(limit time.Duration, stdin io.Reader, name string, args ...string) (int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	command := exec.CommandContext(ctx, name, args...)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.Stdin = stdin
	err := command.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return -1, fmt.Errorf("%s timed out after %s", name, limit)
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return exitErr.ExitCode(), fmt.Errorf("%s exited %d", name, exitErr.ExitCode())
		}
		return -1, err
	}
	return 0, nil
}
