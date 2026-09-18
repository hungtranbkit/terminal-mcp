package proto

import (
	"errors"
	"net/url"
	"strings"
	"testing"
)

const allowedCtl = "https://terminal-dashboard.example.net"

var allowed = []string{allowedCtl, "http://192.168.1.109:8766"}

func enrollURL(handle, controller string) string {
	return "terminalmcp://enroll?handle=" + handle + "&controller=" + url.QueryEscape(controller)
}

func TestWellFormedEnrollParses(t *testing.T) {
	req, err := Parse(enrollURL(strings.Repeat("a", 32), allowedCtl), allowed)
	if err != nil {
		t.Fatalf("expected success, got %v", err)
	}
	if req.Action != ActionEnroll || req.Handle != strings.Repeat("a", 32) || req.Controller != allowedCtl {
		t.Fatalf("unexpected parse: %+v", req)
	}
	if _, present := req.LogFields()["handle"]; present {
		t.Fatal("LogFields must never carry the handle")
	}
}

// The property that matters most: no parameter through which a page can
// hand the helper something to execute.
func TestNoFieldCanCarryACommand(t *testing.T) {
	base := enrollURL(strings.Repeat("a", 32), allowedCtl)
	for _, extra := range []string{
		"&cmd=calc.exe", "&script=evil.ps1", "&exec=whoami",
		"&args=-Command+rm", "&package=evil", "&url=http://evil/x.ps1",
		"&powershell=iex", "&shell=cmd", "&command=del",
	} {
		if _, err := Parse(base+extra, allowed); !errors.Is(err, ErrExtra) {
			t.Fatalf("%s must be refused with ErrExtra, got %v", extra, err)
		}
	}
}

func TestUnknownActionsRefused(t *testing.T) {
	for _, action := range []string{"install", "run", "exec", "enroll2", ".."} {
		raw := "terminalmcp://" + action + "?handle=" + strings.Repeat("a", 32) +
			"&controller=" + url.QueryEscape(allowedCtl)
		if _, err := Parse(raw, allowed); err == nil {
			t.Fatalf("action %q must be refused", action)
		}
	}
}

func TestOnlyThisSchemeAccepted(t *testing.T) {
	for _, raw := range []string{"https://enroll?handle=x", "file://enroll", "terminalmcpx://enroll"} {
		if _, err := Parse(raw, allowed); !errors.Is(err, ErrScheme) {
			t.Fatalf("%s must be refused with ErrScheme, got %v", raw, err)
		}
	}
}

func TestMalformedHandlesRefused(t *testing.T) {
	for _, handle := range []string{
		"", "short", strings.Repeat("g", 32), strings.Repeat("a", 31), strings.Repeat("a", 33),
		"..%2F..%2Fetc", strings.Repeat("a", 30) + "!!",
	} {
		if _, err := Parse(enrollURL(handle, allowedCtl), allowed); err == nil {
			t.Fatalf("handle %q must be refused", handle)
		}
	}
}

// Normalising is safe precisely because the match after it is strict.
func TestHandleCaseNormalisedThenMatchedStrictly(t *testing.T) {
	req, err := Parse(enrollURL(strings.Repeat("A", 32), allowedCtl), allowed)
	if err != nil || req.Handle != strings.Repeat("a", 32) {
		t.Fatalf("uppercase handle should normalise, got %v / %+v", err, req)
	}
}

func TestControllerAllowlist(t *testing.T) {
	for _, hostile := range []string{
		"https://evil.example",
		"https://terminal-dashboard.example.net.evil.com",
		"http://terminal-dashboard.example.net",   // scheme differs
		"https://terminal-dashboard.example.net:8443", // port differs
	} {
		if _, err := Parse(enrollURL(strings.Repeat("a", 32), hostile), allowed); !errors.Is(err, ErrController) {
			t.Fatalf("%s must be refused, got %v", hostile, err)
		}
	}
}

func TestEmptyAllowlistRefusesEverything(t *testing.T) {
	if _, err := Parse(enrollURL(strings.Repeat("a", 32), allowedCtl), nil); !errors.Is(err, ErrController) {
		t.Fatal("a helper with no allowlist must refuse everything")
	}
}

func TestOriginsCompareAsOrigins(t *testing.T) {
	for _, equivalent := range []string{
		"https://terminal-dashboard.example.net/",
		"https://TERMINAL-DASHBOARD.example.net",
		"https://terminal-dashboard.example.net/some/path",
	} {
		req, err := Parse(enrollURL(strings.Repeat("a", 32), equivalent), allowed)
		if err != nil || req.Controller != allowedCtl {
			t.Fatalf("%s should normalise to %s, got %v / %+v", equivalent, allowedCtl, err, req)
		}
	}
}

func TestRepeatedParametersRefused(t *testing.T) {
	raw := "terminalmcp://enroll?handle=" + strings.Repeat("a", 32) +
		"&handle=" + strings.Repeat("b", 32) + "&controller=" + url.QueryEscape(allowedCtl)
	if _, err := Parse(raw, allowed); !errors.Is(err, ErrExtra) {
		t.Fatalf("repeated handle must be refused, got %v", err)
	}
}

func TestPathSegmentsAndFragmentsRefused(t *testing.T) {
	ctl := url.QueryEscape(allowedCtl)
	for _, raw := range []string{
		"terminalmcp://enroll/../../x?handle=" + strings.Repeat("a", 32) + "&controller=" + ctl,
		"terminalmcp://enroll?handle=" + strings.Repeat("a", 32) + "&controller=" + ctl + "#frag",
	} {
		if _, err := Parse(raw, allowed); !errors.Is(err, ErrExtra) {
			t.Fatalf("%s must be refused, got %v", raw, err)
		}
	}
}

func TestRepairAndStatusTakeNoHandle(t *testing.T) {
	ctl := url.QueryEscape(allowedCtl)
	for _, action := range []string{"repair", "status"} {
		if req, err := Parse("terminalmcp://"+action+"?controller="+ctl, allowed); err != nil || req.Handle != "" {
			t.Fatalf("%s without handle should parse, got %v / %+v", action, err, req)
		}
		raw := "terminalmcp://" + action + "?handle=" + strings.Repeat("a", 32) + "&controller=" + ctl
		if _, err := Parse(raw, allowed); err == nil {
			t.Fatalf("%s must not accept a handle", action)
		}
	}
}
