package proto

import (
	"errors"
	"strings"
	"testing"
)

const goodHandle = "0123456789abcdef0123456789abcdef"

// A file name is attacker-controllable: anyone who can hand someone a file
// chooses what it is called. So most of this file is about REFUSING, and
// about the one property that makes the whole idea safe -- that nothing in
// a name can become a command, a path or a flag.

func TestRoundTripCarriesTheSession(t *testing.T) {
	name := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", goodHandle)

	pairing, err := ParseFilename(name)
	if err != nil {
		t.Fatalf("parse %q: %v", name, err)
	}
	if pairing.Controller != "https://ctl.example" {
		t.Errorf("controller = %q", pairing.Controller)
	}
	if pairing.Handle != goodHandle {
		t.Errorf("handle mismatch")
	}
}

func TestTheBytesAreNotWhatCarriesIt(t *testing.T) {
	// Encoding only ever changes the NAME. If this ever starts depending on
	// file contents, the manifest hash and any future Authenticode signature
	// both stop surviving a download.
	name := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", goodHandle)
	if !strings.HasSuffix(name, ".exe") {
		t.Errorf("extension lost: %q", name)
	}
	if !strings.HasPrefix(name, PairPrefix) {
		t.Errorf("prefix lost: %q", name)
	}
}

func TestPortSurvivesTheRoundTrip(t *testing.T) {
	name := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example:8443", goodHandle)
	pairing, err := ParseFilename(name)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if pairing.Controller != "https://ctl.example:8443" {
		t.Errorf("controller = %q", pairing.Controller)
	}
}

func TestALoopbackDevControllerKeepsItsScheme(t *testing.T) {
	name := EncodeFilename("terminal-mcp-bootstrap.exe", "http://127.0.0.1:8766", goodHandle)
	pairing, err := ParseFilename(name)
	if err != nil {
		t.Fatalf("parse %q: %v", name, err)
	}
	if pairing.Controller != "http://127.0.0.1:8766" {
		t.Errorf("controller = %q", pairing.Controller)
	}
}

// -- the browser did something to the name -------------------------------------

func TestADuplicateDownloadStillPairs(t *testing.T) {
	// Chrome and Edge append " (1)" when the file already exists. Refusing
	// over that would send the operator back to copy/paste for no reason.
	base := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", goodHandle)
	renamed := strings.TrimSuffix(base, ".exe") + " (1).exe"

	if _, err := ParseFilename(renamed); err != nil {
		t.Fatalf("deduplicated name refused: %v", err)
	}
}

func TestAPlainDownloadIsNotAnError(t *testing.T) {
	// The supported fallback: install normally, then press Connect again.
	_, err := ParseFilename("terminal-mcp-bootstrap.exe")
	if !errors.Is(err, ErrNoPairing) {
		t.Errorf("want ErrNoPairing, got %v", err)
	}
}

func TestAFullyRenamedDownloadFallsBackQuietly(t *testing.T) {
	for _, name := range []string{"setup.exe", "helper.exe", "download(2).exe"} {
		if _, err := ParseFilename(name); err == nil {
			t.Errorf("%q should not pair", name)
		}
	}
}

func TestEncodingDegradesToThePlainNameWhenThereIsNoSession(t *testing.T) {
	for _, args := range [][2]string{{"", goodHandle}, {"https://ctl.example", ""}, {"", ""}} {
		got := EncodeFilename("terminal-mcp-bootstrap.exe", args[0], args[1])
		if got != "terminal-mcp-bootstrap.exe" {
			t.Errorf("EncodeFilename(%q,%q) = %q", args[0], args[1], got)
		}
	}
}

func TestEncodingRefusesToEmbedAMalformedHandle(t *testing.T) {
	got := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", "nope")
	if got != "terminal-mcp-bootstrap.exe" {
		t.Errorf("a bad handle must not reach a file name: %q", got)
	}
}

// -- handles are opaque, and checked exactly ------------------------------------

func TestMalformedHandlesRefusedInFilename(t *testing.T) {
	for _, handle := range []string{
		"",                                  // empty
		"short",                             // too short
		goodHandle + "0",                    // too long
		"0123456789abcdef0123456789abcdeZ",  // non-hex
		"0123456789ABCDEF0123456789ABCDEF0", // uppercase AND too long
		"../../windows/system32",            // a path, hopefully
		"; shutdown /r",                     // a command, hopefully
	} {
		name := PairPrefix + PairSeparator + "ctl.example" + PairSeparator + handle + ".exe"
		if _, err := ParseFilename(name); err == nil {
			t.Errorf("handle %q was accepted", handle)
		}
	}
}

func TestFilenameHandleCaseIsNormalisedThenMatchedStrictly(t *testing.T) {
	upper := strings.ToUpper(goodHandle)
	name := PairPrefix + PairSeparator + "ctl.example" + PairSeparator + upper + ".exe"

	pairing, err := ParseFilename(name)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if pairing.Handle != goodHandle {
		t.Errorf("handle not normalised: %q", pairing.Handle)
	}
}

func TestARefusedHandleIsNeverEchoedBack(t *testing.T) {
	// A malformed handle is still a credential-shaped string. It must not
	// reach an error message, which reaches a console, a log, a screenshot.
	secret := "deadbeefdeadbeefdeadbeefdeadbeefEXTRA"
	name := PairPrefix + PairSeparator + "ctl.example" + PairSeparator + secret + ".exe"

	_, err := ParseFilename(name)
	if err == nil {
		t.Fatal("expected refusal")
	}
	if strings.Contains(err.Error(), "deadbeef") {
		t.Errorf("error leaked the handle: %v", err)
	}
}

// -- the origin is where the real risk lives -------------------------------------

func TestPlaintextHttpToAPublicHostIsRefused(t *testing.T) {
	// The helper is about to install a LocalSystem service pointed at
	// whatever this says. Over http, that instruction is on the open wire.
	name := PairPrefix + PairSeparator + "http-evil.example" + PairSeparator + goodHandle + ".exe"

	_, err := ParseFilename(name)
	if !errors.Is(err, ErrPairingOrigin) {
		t.Errorf("want ErrPairingOrigin, got %v", err)
	}
}

func TestPlaintextHttpToPrivateAddressesIsAllowedForDevelopment(t *testing.T) {
	for _, host := range []string{"127.0.0.1-p8766", "localhost-p8766", "192.168.1.9-p8766"} {
		name := PairPrefix + PairSeparator + "http-" + host + PairSeparator + goodHandle + ".exe"
		if _, err := ParseFilename(name); err != nil {
			t.Errorf("%q refused: %v", host, err)
		}
	}
}

func TestAnEmptyHostIsRefused(t *testing.T) {
	name := PairPrefix + PairSeparator + "" + PairSeparator + goodHandle + ".exe"
	if _, err := ParseFilename(name); err == nil {
		t.Error("empty host accepted")
	}
}

func TestNoFieldCanCarryACommandOrAPath(t *testing.T) {
	// The property the whole design rests on: the parser returns two typed
	// fields. Nothing it returns can be executed, and nothing that looks
	// like a flag or a path survives validation.
	for _, host := range []string{
		"ctl.example/../../etc",
		"ctl.example;calc.exe",
		"-controller",
		"ctl.example -session x",
		"C:\\Windows\\System32",
	} {
		name := PairPrefix + PairSeparator + host + PairSeparator + goodHandle + ".exe"
		pairing, err := ParseFilename(name)
		if err != nil {
			continue // refused outright, which is the preferred outcome
		}
		if strings.ContainsAny(pairing.Controller, " ;\\") ||
			strings.Contains(pairing.Controller, "..") {
			t.Errorf("host %q produced controller %q", host, pairing.Controller)
		}
	}
}

func TestAnUnexpectedPrefixIsRefused(t *testing.T) {
	// Only this product's own name pairs. A file called something else that
	// happens to have two separators does not.
	name := "totally-legit-installer" + PairSeparator + "ctl.example" + PairSeparator + goodHandle + ".exe"
	if _, err := ParseFilename(name); !errors.Is(err, ErrPairingShape) {
		t.Errorf("want ErrPairingShape, got %v", err)
	}
}

func TestExtraSegmentsAreRefused(t *testing.T) {
	name := PairPrefix + PairSeparator + "ctl.example" + PairSeparator + goodHandle +
		PairSeparator + "extra.exe"
	if _, err := ParseFilename(name); err == nil {
		t.Error("a four-segment name was accepted")
	}
}

func TestAFullPathIsReducedToItsBaseName(t *testing.T) {
	// os.Executable() returns a full path; only the file name pairs.
	full := `C:\Users\op\Downloads\` +
		EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", goodHandle)
	if _, err := ParseFilename(full); err != nil {
		t.Fatalf("full path refused: %v", err)
	}
}

// -- replay is the controller's job, and this must not pretend otherwise ---------

func TestParsingTheSameNameTwiceIsNotReplayProtection(t *testing.T) {
	// Parsing is pure. A handle is spent when the CONTROLLER redeems it --
	// single-use and 120s there -- and nothing here may imply a second
	// local parse is somehow refused. This test exists so that stays true
	// if someone adds caching later.
	name := EncodeFilename("terminal-mcp-bootstrap.exe", "https://ctl.example", goodHandle)

	first, err1 := ParseFilename(name)
	second, err2 := ParseFilename(name)

	if err1 != nil || err2 != nil {
		t.Fatalf("errors: %v %v", err1, err2)
	}
	if first != second {
		t.Error("parsing is not pure")
	}
}
