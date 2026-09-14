package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// downloadSetupScript fetches windows-setup.ps1 from the controller this
// helper is BOUND to -- never from a URL supplied by whoever triggered
// the protocol. That is the difference between "fetch from my controller"
// and "fetch whatever I am told to run".
func downloadSetupScript(controller string) error {
	client := &http.Client{Timeout: 60 * time.Second}
	response, err := client.Get(controller + "/enroll/windows-setup.ps1")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("controller returned HTTP %d", response.StatusCode)
	}
	// 4 MB ceiling: the real script is ~55 KB, and an unbounded read from
	// the network into a file the service then executes is not something
	// to leave open.
	body, err := io.ReadAll(io.LimitReader(response.Body, 4<<20))
	if err != nil {
		return err
	}
	target := filepath.Join(programDataDir(), "windows-setup.ps1")
	if err := os.MkdirAll(programDataDir(), 0o700); err != nil {
		return err
	}
	return writeAtomic(target, body, 0o600)
}
