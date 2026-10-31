package main

import (
	"fmt"
	"net/url"
	"strings"
)

// setupDashboardOrigin keeps owner approval separate from machine enrollment.
func setupDashboardOrigin(controller, dashboard string) (string, error) {
	if dashboard == "" {
		dashboard = controller
	}
	u, err := url.Parse(dashboard)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("setup dashboard must be a trusted HTTPS origin")
	}
	return "https://" + strings.ToLower(u.Host), nil
}
