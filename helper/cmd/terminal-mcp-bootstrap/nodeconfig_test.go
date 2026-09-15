package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The contract these defend, in one sentence: what this helper leaves on
// disk must be what windows-setup.ps1's own first run would have left,
// because -Repair gates on it with
//
//     $script:EnrollmentOk = [bool]($bootstrap -and (Test-Path $TokenFile))
//
// The old helper wrote the raw redeem response to node.json and no token
// file at all, so that gate was false and every dependent step -- SSH key,
// firewall, rescue and the HEARTBEAT TASK -- silently skipped. The node
// registered and then never heartbeat.

const testToken = "aaaabbbbccccddddeeeeffff000011112222333344445555666677778888999a"

// redeemFixture mirrors _consume_record()'s return dict in
// terminal_mcp/node_onboarding.py, field for field.
func redeemFixture() map[string]any {
	return map[string]any{
		"node_id":      "win-work",
		"display_name": "win-work",
		"profile":      "ai_coding",
		"profile_detail": map[string]any{
			"id": "ai_coding", "label": "AI Coding", "needs_signin": true,
		},
		"controller_url":             "https://terminal-bootstrap.example.net",
		"heartbeat_path":             "/dashboard/api/nodes/win-work/heartbeat",
		"node_token":                 testToken,
		"heartbeat_interval_seconds": 30,
		"agent_port":                 8790,
		"ssh": map[string]any{
			"authorized_key":          "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI controller",
			"authorized_key_reason":   nil,
			"firewall_cidrs":          []any{"100.64.0.0/10"},
			"password_authentication": false,
		},
		"tailscale": map[string]any{
			"enabled": true, "reason": nil, "auth_key": "tskey-auth-SECRET-VALUE",
			"tags": []any{}, "unattended": true, "login_server": nil,
		},
		"rescue": map[string]any{"configured": false, "reason": "no_gateway"},
	}
}

func withState(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("ProgramData", dir)
	return filepath.Join(dir, "TerminalMCP")
}

func readConfig(t *testing.T, path string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return parsed
}

// -- the canonical split ----------------------------------------------------

func TestTheTokenIsRemovedFromTheConfigNotJustShadowed(t *testing.T) {
	config, token, err := canonicalNodeConfig(redeemFixture(), "https://dialed.example")
	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	if token != testToken {
		t.Fatal("the token must be returned for separate persistence")
	}
	if _, present := config["node_token"]; present {
		t.Fatal("node_token must not remain in the config")
	}
	// And not merely blanked -- the key itself is gone.
	encoded, _ := json.Marshal(config)
	if strings.Contains(string(encoded), testToken) {
		t.Fatal("the token value must not appear anywhere in the config")
	}
}

func TestTheTailscaleAuthKeyNeverReachesDisk(t *testing.T) {
	// Same rule the installer applies: "used once, below, and never
	// written to disk".
	config, _, err := canonicalNodeConfig(redeemFixture(), "https://dialed.example")
	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	encoded, _ := json.Marshal(config)
	if strings.Contains(string(encoded), "tskey-auth-SECRET-VALUE") {
		t.Fatal("the Tailscale auth key must be stripped")
	}
	tailscale := config["tailscale"].(map[string]any)
	if _, present := tailscale["auth_key"]; present {
		t.Fatal("tailscale.auth_key must be removed")
	}
	// The rest of the tailscale block survives -- it is diagnostics.
	if tailscale["enabled"] != true {
		t.Fatal("tailscale.enabled must be preserved")
	}
}

func TestTheTwoFieldsTheScriptAddsArePresent(t *testing.T) {
	// windows_onboarding.py adds controller_url and controller_key to
	// $safe before writing node.json. -Uninstall reads controller_key to
	// remove this controller's key and leave every other key alone.
	config, _, err := canonicalNodeConfig(redeemFixture(), "https://dialed.example")
	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	if config["controller_url"] != "https://terminal-bootstrap.example.net" {
		t.Fatalf("controller_url = %v", config["controller_url"])
	}
	if config["controller_key"] != "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI controller" {
		t.Fatalf("controller_key = %v", config["controller_key"])
	}
}

func TestTheDialedOriginIsUsedOnlyWhenThePayloadCarriesNone(t *testing.T) {
	payload := redeemFixture()
	delete(payload, "controller_url")

	config, _, err := canonicalNodeConfig(payload, "https://dialed.example")

	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	if config["controller_url"] != "https://dialed.example" {
		t.Fatalf("controller_url = %v", config["controller_url"])
	}
}

func TestEveryFieldTheScriptAndAgentReadSurvives(t *testing.T) {
	// node_id (heartbeat task + repair), controller_url (heartbeat URL,
	// deregister), controller_key (uninstall). Plus the diagnostics the
	// installer also keeps.
	config, _, err := canonicalNodeConfig(redeemFixture(), "")
	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	for _, key := range []string{
		"node_id", "controller_url", "controller_key", "display_name",
		"profile", "heartbeat_path", "heartbeat_interval_seconds",
		"agent_port", "ssh", "tailscale", "rescue",
	} {
		if _, present := config[key]; !present {
			t.Fatalf("%s must survive into node.json", key)
		}
	}
}

func TestAPayloadMissingWhatMattersIsRefusedNotHalfWritten(t *testing.T) {
	for name, mutate := range map[string]func(map[string]any){
		"no node_id":    func(p map[string]any) { delete(p, "node_id") },
		"empty node_id": func(p map[string]any) { p["node_id"] = "" },
		"no token":      func(p map[string]any) { delete(p, "node_token") },
		"empty token":   func(p map[string]any) { p["node_token"] = "   " },
	} {
		payload := redeemFixture()
		mutate(payload)
		if _, _, err := canonicalNodeConfig(payload, "https://x"); err == nil {
			t.Fatalf("%s: want an error", name)
		}
	}
	if _, _, err := canonicalNodeConfig(nil, "https://x"); err == nil {
		t.Fatal("nil payload: want an error")
	}
}

func TestTheCallersMapIsNotMutated(t *testing.T) {
	payload := redeemFixture()
	if _, _, err := canonicalNodeConfig(payload, "https://x"); err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}
	if payload["node_token"] != testToken {
		t.Fatal("the caller's payload must be left alone")
	}
}

// -- persistence, and the -Repair precondition ------------------------------

func TestPersistLeavesExactlyWhatRepairRequires(t *testing.T) {
	stateDir := withState(t)
	config, token, err := canonicalNodeConfig(redeemFixture(), "https://dialed.example")
	if err != nil {
		t.Fatalf("canonicalNodeConfig: %v", err)
	}

	if err := persistNodeConfig(config, token); err != nil {
		t.Fatalf("persistNodeConfig: %v", err)
	}

	// THE precondition: $bootstrap parses AND $TokenFile exists.
	onDisk := readConfig(t, filepath.Join(stateDir, "node.json"))
	if onDisk["node_id"] != "win-work" {
		t.Fatalf("node.json must parse with a node_id, got %v", onDisk["node_id"])
	}
	tokenBytes, err := os.ReadFile(filepath.Join(stateDir, "node.token"))
	if err != nil {
		t.Fatalf("node.token must exist -- this is what -Repair gates on: %v", err)
	}
	if string(tokenBytes) != testToken {
		t.Fatal("node.token must hold the raw token")
	}
	// The script reads it with .Trim(), and writes it -NoNewline; matching
	// that exactly keeps the two paths byte-identical.
	if strings.ContainsAny(string(tokenBytes), "\r\n") {
		t.Fatal("node.token must have no trailing newline")
	}
}

func TestThePersistedConfigNeverContainsTheToken(t *testing.T) {
	stateDir := withState(t)
	config, token, _ := canonicalNodeConfig(redeemFixture(), "https://x")
	if err := persistNodeConfig(config, token); err != nil {
		t.Fatalf("persistNodeConfig: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(stateDir, "node.json"))
	if err != nil {
		t.Fatalf("read node.json: %v", err)
	}
	if strings.Contains(string(raw), testToken) {
		t.Fatal("the token must never be written into node.json")
	}
	if strings.Contains(string(raw), "node_token") {
		t.Fatal("the node_token key must not appear in node.json")
	}
}

func TestAStateDirectoryPathWithSpacesWorks(t *testing.T) {
	// C:\Program Files and C:\ProgramData are the real cases; a space in
	// the path must not break the write or the ACL call.
	base := t.TempDir()
	spaced := filepath.Join(base, "Program Data With Spaces")
	if err := os.MkdirAll(spaced, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	t.Setenv("ProgramData", spaced)

	config, token, _ := canonicalNodeConfig(redeemFixture(), "https://x")
	if err := persistNodeConfig(config, token); err != nil {
		t.Fatalf("persistNodeConfig with spaces: %v", err)
	}

	if _, err := os.Stat(filepath.Join(spaced, "TerminalMCP", "node.token")); err != nil {
		t.Fatalf("node.token must exist under a spaced path: %v", err)
	}
	if !strings.Contains(nodeTokenPath(), "Program Data With Spaces") {
		t.Fatalf("nodeTokenPath did not honour the spaced base: %s", nodeTokenPath())
	}
}

// -- migrating what older helper builds left behind -------------------------

func TestAnOldRawPayloadOnDiskIsSanitisedAndItsTokenRecovered(t *testing.T) {
	stateDir := withState(t)
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// Exactly what helper 0.2.1/0.2.2 wrote: the raw payload, token and
	// all, with no node.token beside it.
	raw, _ := json.MarshalIndent(redeemFixture(), "", "  ")
	if err := os.WriteFile(filepath.Join(stateDir, "node.json"), raw, 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}

	migrated, err := sanitizeExistingNodeConfig()

	if err != nil {
		t.Fatalf("sanitizeExistingNodeConfig: %v", err)
	}
	if !migrated {
		t.Fatal("a config with an embedded token must be reported as migrated")
	}
	tokenBytes, err := os.ReadFile(filepath.Join(stateDir, "node.token"))
	if err != nil {
		t.Fatalf("the token must be recovered into node.token: %v", err)
	}
	if string(tokenBytes) != testToken {
		t.Fatal("the recovered token must match")
	}
	after, _ := os.ReadFile(filepath.Join(stateDir, "node.json"))
	if strings.Contains(string(after), testToken) || strings.Contains(string(after), "node_token") {
		t.Fatal("the token must be removed from node.json on migration")
	}
	if strings.Contains(string(after), "tskey-auth-SECRET-VALUE") {
		t.Fatal("the Tailscale auth key must be stripped on migration too")
	}
	// node_id must survive, or -Repair loses the node it is repairing.
	if readConfig(t, filepath.Join(stateDir, "node.json"))["node_id"] != "win-work" {
		t.Fatal("node_id must survive migration")
	}
}

func TestMigrationNeverOverwritesATokenTheInstallerAlreadyWrote(t *testing.T) {
	stateDir := withState(t)
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	raw, _ := json.MarshalIndent(redeemFixture(), "", "  ")
	_ = os.WriteFile(filepath.Join(stateDir, "node.json"), raw, 0o600)
	// An authoritative token already on disk, from the installer.
	_ = os.WriteFile(filepath.Join(stateDir, "node.token"), []byte("installer-written-token"), 0o600)

	if _, err := sanitizeExistingNodeConfig(); err != nil {
		t.Fatalf("sanitizeExistingNodeConfig: %v", err)
	}

	tokenBytes, _ := os.ReadFile(filepath.Join(stateDir, "node.token"))
	if string(tokenBytes) != "installer-written-token" {
		t.Fatal("an existing node.token is authoritative and must not be replaced")
	}
}

func TestMigrationIsAQuietNoOpWhenThereIsNothingToFix(t *testing.T) {
	stateDir := withState(t)
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	// No file at all.
	if migrated, err := sanitizeExistingNodeConfig(); migrated || err != nil {
		t.Fatalf("missing config: want (false, nil), got (%v, %v)", migrated, err)
	}
	// An already-canonical config.
	config, token, _ := canonicalNodeConfig(redeemFixture(), "https://x")
	if err := persistNodeConfig(config, token); err != nil {
		t.Fatalf("persist: %v", err)
	}
	if migrated, err := sanitizeExistingNodeConfig(); migrated || err != nil {
		t.Fatalf("canonical config: want (false, nil), got (%v, %v)", migrated, err)
	}
	// Something that is not ours at all.
	_ = os.WriteFile(filepath.Join(stateDir, "node.json"), []byte("not json"), 0o600)
	if migrated, err := sanitizeExistingNodeConfig(); migrated || err != nil {
		t.Fatalf("unparseable config: want (false, nil), got (%v, %v)", migrated, err)
	}
}
