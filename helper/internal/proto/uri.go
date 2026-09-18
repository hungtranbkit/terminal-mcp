// Package proto validates terminalmcp:// URLs.
//
// This is the helper's entire external attack surface. Windows hands a
// registered handler whatever URL a page asks it to, and the handler's
// job is to reach a service running as LocalSystem. So the grammar is
// tiny and the parser is suspicious:
//
//	terminalmcp://enroll?handle=<32 hex>&controller=<http(s) origin>
//
// There is deliberately no field carrying a command, a script, a path, an
// argument list, a package name, or a URL to fetch code from. A helper
// that accepted one would be a remote code execution primitive dressed as
// a convenience.
//
// These rules mirror terminal_mcp/bootstrap_protocol.py exactly, and the
// table-driven tests in uri_test.go mirror its test file. Two
// implementations of one grammar is a drift risk, so they are tested
// against the same cases.
package proto

import (
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
)

const Scheme = "terminalmcp"

// Closed set. An unknown action is refused, never ignored.
const (
	ActionEnroll = "enroll"
	ActionRepair = "repair"
	ActionStatus = "status"
)

var actions = map[string]bool{ActionEnroll: true, ActionRepair: true, ActionStatus: true}

var (
	ErrScheme     = errors.New("bad_scheme")
	ErrAction     = errors.New("unknown_action")
	ErrHandle     = errors.New("bad_handle")
	ErrController = errors.New("controller_not_allowed")
	ErrExtra      = errors.New("unexpected_parameter")
	ErrMalformed  = errors.New("malformed_url")
)

var handleRe = regexp.MustCompile(`^[0-9a-f]{32}$`)

// Request is the whole of what a URL may ask for.
type Request struct {
	Action     string
	Handle     string
	Controller string
}

// LogFields deliberately omits Handle: it is a credential for its 120
// seconds of life and has no business in a log line.
func (r Request) LogFields() map[string]any {
	return map[string]any{
		"action":     r.Action,
		"controller": r.Controller,
		"has_handle": r.Handle != "",
	}
}

// NormalizeOrigin reduces a URL to scheme://host[:port], lower-cased.
// Comparing origins as raw strings is how allowlists get bypassed:
// https://ctl.example/ and https://CTL.example are the same origin and
// must compare equal, while https://ctl.example.evil.com must not.
func NormalizeOrigin(value string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil {
		return "", fmt.Errorf("%w: %v", ErrController, err)
	}
	scheme := strings.ToLower(parsed.Scheme)
	if (scheme != "http" && scheme != "https") || parsed.Host == "" {
		return "", fmt.Errorf("%w: controller must be an http(s) origin", ErrController)
	}
	return strings.ToLower(scheme + "://" + parsed.Host), nil
}

// Parse validates a terminalmcp:// URL against the helper's install-time
// controller allowlist. An empty allowlist refuses everything: a helper
// that does not know which controller owns it must do nothing, rather
// than trust whoever asked first.
func Parse(raw string, allowedControllers []string) (Request, error) {
	var zero Request
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return zero, fmt.Errorf("%w: %v", ErrMalformed, err)
	}
	if !strings.EqualFold(parsed.Scheme, Scheme) {
		return zero, fmt.Errorf("%w: expected %s://", ErrScheme, Scheme)
	}

	// Windows may deliver the action as host ("terminalmcp://enroll?x") or
	// as an opaque path ("terminalmcp:enroll").
	action := strings.ToLower(strings.TrimSpace(parsed.Host))
	if action == "" {
		action = strings.ToLower(strings.Trim(strings.TrimSpace(parsed.Opaque+parsed.Path), "/"))
	} else if strings.Trim(parsed.Path, "/") != "" {
		// A path AFTER the action is not in the grammar. Refuse rather
		// than ignore -- ignoring is how traversal payloads ride along.
		return zero, fmt.Errorf("%w: no path segments are accepted", ErrExtra)
	}
	if !actions[action] {
		return zero, fmt.Errorf("%w: action must be enroll, repair or status", ErrAction)
	}
	if parsed.Fragment != "" {
		return zero, fmt.Errorf("%w: fragments are not accepted", ErrExtra)
	}

	query, err := url.ParseQuery(parsed.RawQuery)
	if err != nil {
		return zero, fmt.Errorf("%w: %v", ErrMalformed, err)
	}
	for key, values := range query {
		if key != "handle" && key != "controller" {
			// The important case: a future cmd=/script= must be a hard
			// refusal, not something a permissive parser drops.
			return zero, fmt.Errorf("%w: unexpected parameter %q", ErrExtra, key)
		}
		if len(values) != 1 {
			// Ambiguity is how one layer reads handle #1 while another
			// reads handle #2.
			return zero, fmt.Errorf("%w: %s given more than once", ErrExtra, key)
		}
	}

	controller, err := NormalizeOrigin(query.Get("controller"))
	if err != nil {
		return zero, err
	}
	allowed := false
	for _, entry := range allowedControllers {
		if strings.TrimSpace(entry) == "" {
			continue
		}
		if normalized, err := NormalizeOrigin(entry); err == nil && normalized == controller {
			allowed = true
			break
		}
	}
	if !allowed {
		return zero, fmt.Errorf("%w: controller is not in this helper's allowlist", ErrController)
	}

	handle := strings.ToLower(strings.TrimSpace(query.Get("handle")))
	if action == ActionEnroll {
		if !handleRe.MatchString(handle) {
			return zero, fmt.Errorf("%w: handle must be 32 lowercase hex characters", ErrHandle)
		}
	} else if handle != "" {
		return zero, fmt.Errorf("%w: %s takes no handle", ErrExtra, action)
	}

	return Request{Action: action, Handle: handle, Controller: controller}, nil
}
