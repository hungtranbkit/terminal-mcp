package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// waitForInstalledNode is the success gate for the one-file flow. A browser
// opening is not success: the owner must approve, the ai_coding config must
// be persisted, and the real local agent must answer health first.
func waitForInstalledNode(limit time.Duration) error {
	deadline := time.Now().Add(limit)
	client := &http.Client{Timeout: 3 * time.Second}
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(filepath.Join(programDataDir(), "install-status.json"))
		if err == nil {
			var config struct {
				Ready         bool   `json:"ready"`
				Profile       string `json:"profile"`
				Endpoint      string `json:"agent_endpoint"`
				Authenticated bool   `json:"authenticated_agent"`
				Heartbeat     bool   `json:"heartbeat_accepted"`
			}
			if json.Unmarshal(raw, &config) == nil && config.Ready && config.Profile == "ai_coding" && config.Authenticated && config.Heartbeat && strings.HasPrefix(config.Endpoint, "http://100.") {
				response, requestErr := client.Get(strings.TrimRight(config.Endpoint, "/") + "/v1/health")
				if requestErr == nil {
					body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
					_ = response.Body.Close()
					if response.StatusCode == http.StatusOK && strings.Contains(string(body), `"status":"ok"`) {
						return nil
					}
				}
			}
		}
		time.Sleep(2 * time.Second)
	}
	return fmt.Errorf("node agent readiness timed out")
}
