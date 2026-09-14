package proto

import (
	"bufio"
	"os"
	"testing"
)

// Cross-language check: the CONTROLLER encodes these names (Python,
// helper_artifact.paired_filename) and the HELPER parses them (Go, here).
// Two implementations of one format drift silently unless something
// compares them, so this reads names produced by the Python side and
// asserts the Go side agrees about each one.
//
// Skipped unless TMCP_XCHECK_NAMES points at that file, so the ordinary
// suite has no external dependency.
func TestControllerEncodedNamesParseHere(t *testing.T) {
	path := os.Getenv("TMCP_XCHECK_NAMES")
	if path == "" {
		t.Skip("set TMCP_XCHECK_NAMES to a file of controller-encoded names")
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer file.Close()

	expect := map[string]string{
		"terminal-mcp-bootstrap__terminal-bootstrap.mesflow.net__0123456789abcdef0123456789abcdef.exe": "https://terminal-bootstrap.mesflow.net",
		"terminal-mcp-bootstrap__http-192.168.1.109-p8766__0123456789abcdef0123456789abcdef.exe":       "http://192.168.1.109:8766",
		"terminal-mcp-bootstrap__ctl.example-p8443__0123456789abcdef0123456789abcdef.exe":              "https://ctl.example:8443",
		// A plaintext-http PUBLIC origin is encodable but must be refused
		// on parse: the helper is about to trust it with an install.
		"terminal-mcp-bootstrap__http-testserver__0123456789abcdef0123456789abcdef.exe": "",
		"terminal-mcp-bootstrap.exe": "",
	}

	seen := 0
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		name := scanner.Text()
		if name == "" {
			continue
		}
		want, known := expect[name]
		if !known {
			t.Errorf("controller produced an unexpected name: %q", name)
			continue
		}
		seen++
		pairing, err := ParseFilename(name)
		if want == "" {
			if err == nil {
				t.Errorf("%q should have been refused, got %q", name, pairing.Controller)
			}
			continue
		}
		if err != nil {
			t.Errorf("%q refused: %v", name, err)
			continue
		}
		if pairing.Controller != want {
			t.Errorf("%q -> %q, want %q", name, pairing.Controller, want)
		}
	}
	if seen != len(expect) {
		t.Errorf("checked %d names, expected %d", seen, len(expect))
	}
}
