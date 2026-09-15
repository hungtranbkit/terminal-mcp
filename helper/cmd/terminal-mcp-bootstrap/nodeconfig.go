package main

// The on-disk contract between this helper and windows-setup.ps1.
//
// WHY THIS FILE EXISTS. The helper used to write the redeem response
// verbatim to node.json and nothing else. windows-setup.ps1's own first
// run writes TWO files and a DIFFERENT shape:
//
//   node.json   the payload with node_token REMOVED (and the Tailscale
//               auth key removed), plus controller_url and controller_key
//               added. Its comment: "the config is fine to read for
//               diagnostics, the token never is."
//   node.token  the raw token, ASCII, no trailing newline, then icacls
//               /inheritance:r granting only SYSTEM and Administrators.
//
// -Repair gates everything on that second file:
//
//   $script:EnrollmentOk = [bool]($bootstrap -and (Test-Path $TokenFile))
//
// so a helper that never wrote node.token could not possibly succeed: the
// gate was false, and SSH key, firewall, rescue AND the heartbeat task all
// SKIPped. The node registered and then never heartbeat, which is why its
// agent_types stayed empty and no Agent Type could be chosen for it.
//
// Writing the token into node.json was also a real exposure: Go's chmod on
// Windows only toggles the read-only bit and sets no NTFS ACL, so the
// secret sat in a file the script deliberately keeps readable.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// redeemPayload is the typed view of what /dashboard/api/enroll/redeem
// returns. Only the fields this helper must reason about are named; the
// rest of the response is preserved verbatim in node.json for diagnostics,
// which is what the installer does too.
type redeemPayload struct {
	NodeID        string `json:"node_id"`
	DisplayName   string `json:"display_name"`
	Profile       string `json:"profile"`
	ControllerURL string `json:"controller_url"`
	HeartbeatPath string `json:"heartbeat_path"`
	NodeToken     string `json:"node_token"`
	AgentPort     int    `json:"agent_port"`
	SSH           struct {
		AuthorizedKey string `json:"authorized_key"`
	} `json:"ssh"`
}

// nodeTokenPath is $TokenFile: $env:ProgramData\TerminalMCP\node.token.
func nodeTokenPath() string { return filepath.Join(programDataDir(), "node.token") }

// canonicalNodeConfig splits one redeem response into exactly what the
// installer's first run would have left on disk.
//
// Returns the config to write and the token to write separately. The token
// is REMOVED from the config, never merely shadowed: a caller that
// forgot to use the second return value must not end up writing a config
// that still carries the secret.
//
// `dialed` is the origin this helper actually talked to, used only when the
// payload carries no controller_url of its own -- the installer resolves
// its own and stamps it the same way.
func canonicalNodeConfig(raw map[string]any, dialed string) (map[string]any, string, error) {
	if raw == nil {
		return nil, "", fmt.Errorf("empty bootstrap payload")
	}
	typed := redeemPayload{}
	// Round-trip through the typed view so a malformed field is caught
	// here rather than three steps later in PowerShell.
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, "", fmt.Errorf("re-encode bootstrap payload: %w", err)
	}
	if err := json.Unmarshal(encoded, &typed); err != nil {
		return nil, "", fmt.Errorf("bootstrap payload is not the expected shape: %w", err)
	}
	if strings.TrimSpace(typed.NodeID) == "" {
		return nil, "", fmt.Errorf("bootstrap payload has no node_id")
	}
	token := strings.TrimSpace(typed.NodeToken)
	if token == "" {
		return nil, "", fmt.Errorf("bootstrap payload has no node_token")
	}

	// Copy so the caller's map is never mutated underneath it.
	config := make(map[string]any, len(raw)+2)
	for key, value := range raw {
		config[key] = value
	}
	// 1. The secret leaves the config entirely.
	delete(config, "node_token")
	// 2. The Tailscale auth key is used once by the installer and is
	//    never written to disk -- same rule the script applies.
	if tailscale, ok := config["tailscale"].(map[string]any); ok {
		scrubbed := make(map[string]any, len(tailscale))
		for key, value := range tailscale {
			if key == "auth_key" {
				continue
			}
			scrubbed[key] = value
		}
		config["tailscale"] = scrubbed
	}
	// 3. The two fields the script ADDS on a first run. controller_key is
	//    what -Uninstall uses to remove this controller's key from
	//    administrators_authorized_keys without touching anyone else's.
	controller := strings.TrimSpace(typed.ControllerURL)
	if controller == "" {
		controller = strings.TrimSpace(dialed)
	}
	config["controller_url"] = controller
	config["controller_key"] = typed.SSH.AuthorizedKey
	return config, token, nil
}

// persistNodeConfig writes both files the installer expects, config first
// and token second, each atomically.
//
// Order matters on the failure path: a token with no config is inert,
// whereas a config with no token is exactly the state that made -Repair
// skip everything silently.
func persistNodeConfig(config map[string]any, token string) error {
	if err := os.MkdirAll(programDataDir(), 0o700); err != nil {
		return fmt.Errorf("create state directory: %w", err)
	}
	encoded, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return fmt.Errorf("encode node config: %w", err)
	}
	if err := writeAtomic(nodeConfigPath(), encoded, 0o600); err != nil {
		return fmt.Errorf("write node config: %w", err)
	}
	// No trailing newline: the script reads this with Get-Content -Raw and
	// .Trim(), but the installer writes it -NoNewline and a byte-identical
	// file is one less thing that can differ between the two paths.
	if err := writeAtomic(nodeTokenPath(), []byte(token), 0o600); err != nil {
		return fmt.Errorf("write node token: %w", err)
	}
	// Best-effort, and deliberately not fatal: the file is already written
	// with the tightest mode Go offers, and failing the whole install
	// because icacls was unavailable would trade a real outcome for a
	// hardening step.
	if err := restrictTokenACL(nodeTokenPath()); err != nil {
		fmt.Fprintf(os.Stderr, "      ! could not tighten ACL on node.token: %v\n", err)
	}
	return nil
}

// sanitizeExistingNodeConfig repairs what earlier helper builds left on
// disk: a node.json with node_token embedded and no node.token beside it.
//
// Runs before the installer on every run, because the machine being fixed
// is precisely the machine that already has the bad file. It never
// overwrites a token file that already exists -- that one came from the
// installer itself and is authoritative.
func sanitizeExistingNodeConfig() (bool, error) {
	raw, err := os.ReadFile(nodeConfigPath())
	if err != nil {
		return false, nil // nothing on disk yet is the normal case
	}
	var existing map[string]any
	if err := json.Unmarshal(raw, &existing); err != nil {
		return false, nil // not ours to rewrite
	}
	token, embedded := existing["node_token"].(string)
	if !embedded || strings.TrimSpace(token) == "" {
		return false, nil
	}
	delete(existing, "node_token")
	if tailscale, ok := existing["tailscale"].(map[string]any); ok {
		delete(tailscale, "auth_key")
	}
	encoded, err := json.MarshalIndent(existing, "", "  ")
	if err != nil {
		return false, err
	}
	// Write the token out first: if this run dies between the two writes,
	// the machine ends up with a usable pair rather than a config whose
	// secret has been deleted with nowhere else to find it.
	if _, err := os.Stat(nodeTokenPath()); os.IsNotExist(err) {
		if err := writeAtomic(nodeTokenPath(), []byte(strings.TrimSpace(token)), 0o600); err != nil {
			return false, fmt.Errorf("write recovered node token: %w", err)
		}
		if err := restrictTokenACL(nodeTokenPath()); err != nil {
			fmt.Fprintf(os.Stderr, "      ! could not tighten ACL on node.token: %v\n", err)
		}
	}
	if err := writeAtomic(nodeConfigPath(), encoded, 0o600); err != nil {
		return false, fmt.Errorf("rewrite sanitised node config: %w", err)
	}
	return true, nil
}
