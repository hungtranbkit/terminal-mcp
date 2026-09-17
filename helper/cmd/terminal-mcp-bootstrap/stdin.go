package main

// A stdin the installer can read from without blocking or throwing.
//
// windows-setup.ps1 ends with Wait-BeforeClosing, which does
// `Read-Host $Prompt` and, on failure, `catch { Start-Sleep -Seconds 60 }`.
// That prompt exists for a human at a console. The helper is not one: it
// used to give the child no stdin at all, so Read-Host threw and the
// script slept a silent minute on every failure and warning path before
// the helper could report anything.
//
// Feeding endless newlines makes Read-Host return immediately with an
// empty string, on that path and any other prompt the script might add --
// without the helper having to know which prompts exist.

import "io"

// newlineFeeder returns a reader that yields "\n" forever. Never closed
// and never exhausted on purpose: an EOF is what makes Read-Host throw,
// which is the behaviour being fixed.
func newlineFeeder() io.Reader { return endlessNewlines{} }

type endlessNewlines struct{}

func (endlessNewlines) Read(buffer []byte) (int, error) {
	if len(buffer) == 0 {
		return 0, nil
	}
	buffer[0] = '\n'
	return 1, nil
}
