package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// downloadSetupScript is where a live ai_coding enrollment died on
// 2026-09-15: it redeemed, reported installing_service, then GET
// <bootstrap-origin>/enroll/windows-setup.ps1 returned 404 because that
// path was not in the public bootstrap ingress allowlist. Loopback served
// the same path 200. These drive the function against a stand-in for the
// bootstrap hostname so both outcomes are pinned.

const fakeScript = "param(\n    [string] $EnrollmentCode\n)\n# windows-setup.ps1\n"

func bootstrapHost(t *testing.T, status int) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	// Exactly the two paths the ingress now allows, and nothing else: the
	// default handler 404s like the edge catch-all does.
	serve := func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if status != http.StatusOK {
			w.WriteHeader(status)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte(fakeScript))
	}
	mux.HandleFunc("/enroll/windows-setup.ps1", serve)
	mux.HandleFunc("/w", serve)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server
}

func withProgramData(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	// programDataDir() reads ProgramData; pointing it at a temp dir keeps
	// this test from touching anything real.
	t.Setenv("ProgramData", dir)
	return filepath.Join(dir, "TerminalMCP")
}

func TestTheHelperCanFetchTheSetupScriptThroughTheBootstrapHostname(t *testing.T) {
	stateDir := withProgramData(t)
	server := bootstrapHost(t, http.StatusOK)

	if err := downloadSetupScript(server.URL); err != nil {
		t.Fatalf("download through the bootstrap origin must succeed: %v", err)
	}

	saved, err := os.ReadFile(filepath.Join(stateDir, "windows-setup.ps1"))
	if err != nil {
		t.Fatalf("the script must be persisted for the installer to run: %v", err)
	}
	if string(saved) != fakeScript {
		t.Fatal("the saved script must be exactly what the controller served")
	}
}

func TestA404FromTheEdgeIsReportedAsAFailureNotSwallowed(t *testing.T) {
	// The regression itself: before the ingress rule, this is what every
	// paired install got, and it had to surface as an error the helper
	// reports rather than a silent half-install.
	withProgramData(t)
	server := bootstrapHost(t, http.StatusNotFound)

	err := downloadSetupScript(server.URL)

	if err == nil {
		t.Fatal("a 404 must be an error")
	}
	if !strings.Contains(err.Error(), "404") {
		t.Fatalf("the error must name the status, got: %v", err)
	}
}

func TestTheHelperOnlyEverFetchesFromTheOriginItWasPairedWith(t *testing.T) {
	// "fetch from my controller", never "fetch whatever I am told to run".
	stateDir := withProgramData(t)
	var asked []string
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		asked = append(asked, r.Host+r.URL.Path)
		_, _ = w.Write([]byte(fakeScript))
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	if err := downloadSetupScript(server.URL); err != nil {
		t.Fatalf("download: %v", err)
	}

	if len(asked) != 1 {
		t.Fatalf("want exactly one fetch, got %v", asked)
	}
	if !strings.HasSuffix(asked[0], "/enroll/windows-setup.ps1") {
		t.Fatalf("the helper must fetch the setup script path, got %q", asked[0])
	}
	_ = stateDir
}
