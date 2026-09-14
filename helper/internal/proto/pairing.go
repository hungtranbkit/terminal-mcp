// Pairing carried by the download's FILE NAME.
//
// # THE GAP THIS CLOSES
//
// On a machine that has never been onboarded there is no helper yet, so
// there is no terminalmcp:// handler for the web page to hand a session to.
// The page can only offer a download. And a downloaded
// terminal-mcp-bootstrap.exe, double-clicked, knew nothing: main() saw no
// arguments, printed usage to stderr, and exited 2 -- which on a
// double-click is a console window that flashes and disappears. The pending
// enrollment the operator had just created was simply lost, and they were
// back to copying a PowerShell command.
//
// So the controller names the file after the session it belongs to:
//
//	terminal-mcp-bootstrap__<origin>__<handle>.exe
//
// The BYTES are untouched. This matters more than it looks: the artifact is
// hashed against its manifest on every download and will one day be
// Authenticode-signed, and neither survives rewriting the file per request.
// A name is metadata the operator's browser carries for free.
//
// # WHAT THE HELPER MAY BELIEVE
//
// Nothing, without checking. A file name is attacker-controllable -- anyone
// who can hand someone a file can choose what it is called. So:
//
//   - the handle is OPAQUE: 32 hex characters, matched exactly, never
//     interpreted, never logged, never echoed back
//   - the origin must be an https:// origin (loopback and private-range
//     http are allowed for development, and nothing else is)
//   - nothing in the name can become a command, a path, or a flag; the
//     parser returns two typed fields and cannot return anything else
//
// That last property is the point. The worst a hostile file name can do is
// point the helper at a controller that refuses its handle.
//
// # WHEN THE NAME IS GONE
//
// Browsers deduplicate (`...(1).exe`), some corporate proxies rewrite
// downloads entirely, and an operator may simply rename it. A helper that
// found no pairing in its own name is not broken -- it installs, registers
// the protocol, and the page's Connect button works from then on. The
// fallback is the ordinary path, not an error state.
package proto

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"path/filepath"
	"regexp"
	"strings"
)

// Separator: double underscore. Legal on NTFS, never produced by the
// browser's own deduplication suffixes, and visually obvious in a file name
// an operator is about to run.
const PairSeparator = "__"

const PairPrefix = "terminal-mcp-bootstrap"

var (
	ErrNoPairing     = errors.New("no_pairing_in_filename")
	ErrPairingHandle = errors.New("bad_pairing_handle")
	ErrPairingOrigin = errors.New("bad_pairing_origin")
	ErrPairingShape  = errors.New("bad_pairing_shape")
)

// A browser that has already saved this file once appends " (1)", "(2)" and
// so on. Strip it before parsing rather than refusing a perfectly good
// pairing over a duplicate download.
var dedupeSuffix = regexp.MustCompile(`\s*\(\d+\)$`)

// Origins are encoded host-first so the file name stays readable to the
// person about to run it: they can see which controller it came from.
// A port becomes `-p<port>` because ':' is illegal in a Windows file name.
var portSuffix = regexp.MustCompile(`-p(\d{1,5})$`)

// A hostname, and nothing else. NormalizeOrigin alone is not enough here:
// url.Parse happily accepts "ctl.example;calc.exe" as a host, and a file
// name is attacker-controllable, so the host has to be checked against what
// a hostname may actually contain rather than against what a URL parser
// will tolerate. Caught by its own test.
var hostnameRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$`)

// Pairing is what a download's name can carry: where to enrol, and the
// one-time handle that authorises it. Both fields are inert data.
type Pairing struct {
	Controller string // normalised origin, e.g. https://ctl.example
	Handle     string // 32 hex, opaque
}

// EncodeFilename builds the download name for one pending session.
//
// Returns the plain name when either field is missing: a generic download is
// a valid outcome (the operator may have no session yet), not an error.
func EncodeFilename(base, controller, handle string) string {
	if base == "" {
		base = PairPrefix + ".exe"
	}
	if controller == "" || handle == "" {
		return base
	}
	origin, err := NormalizeOrigin(controller)
	if err != nil || !handleRe.MatchString(strings.ToLower(handle)) {
		return base
	}
	parsed, err := url.Parse(origin)
	if err != nil {
		return base
	}
	host := parsed.Hostname()
	if port := parsed.Port(); port != "" {
		host += "-p" + port
	}
	if strings.ToLower(parsed.Scheme) == "http" {
		// Recorded explicitly so the parser never has to GUESS a scheme --
		// guessing https for a dev controller would silently fail to
		// connect, and guessing http for a real one would be worse.
		host = "http-" + host
	}
	stem := strings.TrimSuffix(base, filepath.Ext(base))
	ext := filepath.Ext(base)
	if ext == "" {
		ext = ".exe"
	}
	return stem + PairSeparator + host + PairSeparator + strings.ToLower(handle) + ext
}

// ParseFilename recovers a pairing from the name of the running executable.
//
// Every failure is ErrNoPairing-shaped from the caller's point of view: the
// helper falls back to an ordinary install either way. The distinct errors
// exist so a test can prove WHICH rule refused, not so the helper can treat
// them differently.
func ParseFilename(name string) (Pairing, error) {
	var zero Pairing
	// Split on BOTH separators. The helper parses a Windows path, but this
	// package is also compiled and tested on Linux, where filepath.Base
	// does not treat a backslash as a separator -- so a real Windows path
	// would arrive here whole and fail to match the prefix.
	base := strings.TrimSpace(name)
	if index := strings.LastIndexAny(base, `/\`); index >= 0 {
		base = base[index+1:]
	}
	ext := filepath.Ext(base)
	stem := dedupeSuffix.ReplaceAllString(strings.TrimSuffix(base, ext), "")

	parts := strings.Split(stem, PairSeparator)
	if len(parts) != 3 {
		return zero, fmt.Errorf("%w: %d segments", ErrNoPairing, len(parts))
	}
	if !strings.EqualFold(parts[0], PairPrefix) {
		return zero, fmt.Errorf("%w: unexpected prefix", ErrPairingShape)
	}

	handle := strings.ToLower(parts[2])
	if !handleRe.MatchString(handle) {
		// Deliberately does not include the value: a malformed handle is
		// still a credential-shaped string and does not belong in output.
		return zero, ErrPairingHandle
	}

	host := parts[1]
	scheme := "https"
	if rest, found := strings.CutPrefix(host, "http-"); found {
		scheme, host = "http", rest
	}
	if match := portSuffix.FindStringSubmatch(host); match != nil {
		host = strings.TrimSuffix(host, match[0]) + ":" + match[1]
	}
	if host == "" {
		return zero, fmt.Errorf("%w: empty host", ErrPairingOrigin)
	}
	hostOnly, _, found := strings.Cut(host, ":")
	if !found {
		hostOnly = host
	}
	if !hostnameRe.MatchString(strings.ToLower(hostOnly)) {
		return zero, fmt.Errorf("%w: %q is not a hostname", ErrPairingOrigin, hostOnly)
	}

	origin, err := NormalizeOrigin(scheme + "://" + host)
	if err != nil {
		return zero, fmt.Errorf("%w: %v", ErrPairingOrigin, err)
	}
	if err := requireSafeScheme(origin); err != nil {
		return zero, err
	}
	return Pairing{Controller: origin, Handle: handle}, nil
}

// requireSafeScheme refuses plaintext http to anything that is not this
// machine or a private address.
//
// A handle travelling to a public host over http is a credential on the
// open wire, and the helper is about to install a LocalSystem service on
// whatever that host tells it to. Development against a loopback or
// LAN controller stays possible; everything else must be https.
func requireSafeScheme(origin string) error {
	parsed, err := url.Parse(origin)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrPairingOrigin, err)
	}
	if strings.EqualFold(parsed.Scheme, "https") {
		return nil
	}
	host := parsed.Hostname()
	if strings.EqualFold(host, "localhost") {
		return nil
	}
	ip := net.ParseIP(host)
	if ip != nil && (ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast()) {
		return nil
	}
	return fmt.Errorf("%w: refusing plaintext http to %q", ErrPairingOrigin, host)
}
