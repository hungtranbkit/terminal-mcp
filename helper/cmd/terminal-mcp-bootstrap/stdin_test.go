package main

import (
	"io"
	"testing"
)

// windows-setup.ps1 ends in Wait-BeforeClosing:
//
//     try { Read-Host $Prompt | Out-Null } catch { Start-Sleep -Seconds 60 }
//
// With no stdin wired, Read-Host threw and the script slept a silent
// minute before the helper could report anything -- on every failure and
// warning path.

func TestTheInstallerAlwaysHasALineToRead(t *testing.T) {
	reader := newlineFeeder()
	buffer := make([]byte, 1)
	for attempt := 0; attempt < 5; attempt++ {
		count, err := reader.Read(buffer)
		if err != nil {
			t.Fatalf("read %d returned %v -- an EOF is exactly what makes Read-Host throw", attempt, err)
		}
		if count != 1 || buffer[0] != '\n' {
			t.Fatalf("read %d gave %q", attempt, buffer[:count])
		}
	}
}

func TestTheFeederNeverReportsEOF(t *testing.T) {
	// io.ReadFull over a large buffer must succeed: any EOF here would
	// resurrect the 60-second sleep.
	buffer := make([]byte, 4096)
	if _, err := io.ReadFull(newlineFeeder(), buffer); err != nil {
		t.Fatalf("ReadFull: %v", err)
	}
	for index, character := range buffer {
		if character != '\n' {
			t.Fatalf("byte %d = %q, want a newline", index, character)
		}
	}
}

func TestAZeroLengthReadIsNotAnError(t *testing.T) {
	count, err := newlineFeeder().Read(nil)
	if err != nil || count != 0 {
		t.Fatalf("Read(nil) = (%d, %v), want (0, nil)", count, err)
	}
}
