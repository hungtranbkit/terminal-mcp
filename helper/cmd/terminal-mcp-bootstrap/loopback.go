package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	loopbackAddress  = "127.0.0.1:47562"
	loopbackHost     = "127.0.0.1:47562"
	challengeTTL     = 90 * time.Second
	maxPairBodyBytes = 4096
	challengePath    = "/v1/challenge"
	pairPath         = "/v1/pair"
)

type loopbackChallenge struct {
	Origin    string
	ExpiresAt time.Time
}

type loopbackChallengeResponse struct {
	Product   string    `json:"product"`
	Version   string    `json:"version"`
	Platform  string    `json:"platform"`
	Nonce     string    `json:"nonce"`
	ExpiresAt time.Time `json:"expires_at"`
}

type loopbackPairRequest struct {
	Nonce  string `json:"nonce"`
	Handle string `json:"handle"`
}

type loopbackHandler struct {
	mu                   sync.Mutex
	challenges           map[string]loopbackChallenge
	origins              map[string]struct{}
	allowAnySecureOrigin bool
	pairing              bool
	now                  func() time.Time
	pair                 func(handle, origin string) error
}

func newLoopbackHandler(config Config, pair func(handle, origin string) error) http.Handler {
	return newLoopbackHandlerWithClock(config, pair, time.Now)
}

func newLoopbackHandlerWithClock(config Config, pair func(handle, origin string) error,
	now func() time.Time) http.Handler {
	origins := make(map[string]struct{}, len(config.DashboardOrigins))
	for _, raw := range config.DashboardOrigins {
		if origin, ok := secureDashboardOrigin(raw); ok {
			origins[origin] = struct{}{}
		}
	}
	return &loopbackHandler{
		challenges: make(map[string]loopbackChallenge), origins: origins,
		allowAnySecureOrigin: len(origins) == 0, now: now, pair: pair,
	}
}

func (h *loopbackHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Host != loopbackHost {
		http.Error(w, "loopback host required", http.StatusForbidden)
		return
	}
	origin, ok := secureDashboardOrigin(r.Header.Get("Origin"))
	if !ok || !h.originAllowed(origin) {
		http.Error(w, "dashboard origin refused", http.StatusForbidden)
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", origin)
	w.Header().Set("Vary", "Origin")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
	w.Header().Set("Access-Control-Allow-Private-Network", "true")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")

	if r.Method == http.MethodOptions {
		if r.URL.Path != pairPath || r.Header.Get("Access-Control-Request-Method") != http.MethodPost ||
			r.Header.Get("Access-Control-Request-Private-Network") != "true" {
			http.Error(w, "preflight refused", http.StatusForbidden)
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	switch {
	case r.URL.Path == challengePath && r.Method == http.MethodGet:
		h.issueChallenge(w, origin)
	case r.URL.Path == pairPath && r.Method == http.MethodPost:
		h.pairOnce(w, r, origin)
	default:
		http.NotFound(w, r)
	}
}

func (h *loopbackHandler) issueChallenge(w http.ResponseWriter, origin string) {
	var random [32]byte
	if _, err := rand.Read(random[:]); err != nil {
		http.Error(w, "challenge unavailable", http.StatusServiceUnavailable)
		return
	}
	nonce := hex.EncodeToString(random[:])
	now := h.now()
	h.mu.Lock()
	for key, challenge := range h.challenges {
		if !challenge.ExpiresAt.After(now) {
			delete(h.challenges, key)
		}
	}
	if len(h.challenges) >= 1024 {
		h.mu.Unlock()
		http.Error(w, "too many outstanding challenges", http.StatusTooManyRequests)
		return
	}
	h.challenges[nonce] = loopbackChallenge{Origin: origin, ExpiresAt: now.Add(challengeTTL)}
	h.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(loopbackChallengeResponse{
		Product: ProductName, Version: Version, Platform: runtime.GOOS,
		Nonce: nonce, ExpiresAt: now.Add(challengeTTL),
	})
}

func (h *loopbackHandler) pairOnce(w http.ResponseWriter, r *http.Request, origin string) {
	limited := http.MaxBytesReader(w, r.Body, maxPairBodyBytes)
	decoder := json.NewDecoder(limited)
	decoder.DisallowUnknownFields()
	var body loopbackPairRequest
	if err := decoder.Decode(&body); err != nil {
		http.Error(w, "invalid pairing request", http.StatusBadRequest)
		return
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		http.Error(w, "invalid pairing request", http.StatusBadRequest)
		return
	}
	if len(body.Nonce) != 64 || !isHex(body.Nonce) || !handleLooksValid(body.Handle) {
		http.Error(w, "invalid pairing request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	challenge, exists := h.challenges[body.Nonce]
	if exists {
		delete(h.challenges, body.Nonce)
	}
	h.mu.Unlock()
	if !exists || challenge.Origin != origin || !challenge.ExpiresAt.After(h.now()) {
		http.Error(w, "challenge expired or already used", http.StatusConflict)
		return
	}
	h.mu.Lock()
	if h.pairing {
		h.mu.Unlock()
		http.Error(w, "pairing already in progress", http.StatusConflict)
		return
	}
	h.pairing = true
	h.mu.Unlock()
	if h.pair == nil {
		h.mu.Lock()
		h.pairing = false
		h.mu.Unlock()
		http.Error(w, "pairing unavailable", http.StatusServiceUnavailable)
		return
	}
	go func() {
		err := h.pair(body.Handle, origin)
		h.mu.Lock()
		h.pairing = false
		if err == nil && h.allowAnySecureOrigin {
			h.origins[origin] = struct{}{}
			h.allowAnySecureOrigin = false
		}
		h.mu.Unlock()
	}()
	w.WriteHeader(http.StatusAccepted)
}

func (h *loopbackHandler) originAllowed(origin string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.allowAnySecureOrigin {
		return true
	}
	_, ok := h.origins[origin]
	return ok
}

func isHex(value string) bool {
	_, err := hex.DecodeString(value)
	return err == nil
}

func secureDashboardOrigin(value string) (string, bool) {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" ||
		parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
		return "", false
	}
	scheme := strings.ToLower(parsed.Scheme)
	if scheme != "https" && !(scheme == "http" && isLoopbackHost(parsed.Hostname())) {
		return "", false
	}
	return scheme + "://" + strings.ToLower(parsed.Host), true
}

func isLoopbackHost(host string) bool {
	ip := net.ParseIP(strings.Trim(host, "[]"))
	return ip != nil && ip.IsLoopback() || strings.EqualFold(host, "localhost")
}
