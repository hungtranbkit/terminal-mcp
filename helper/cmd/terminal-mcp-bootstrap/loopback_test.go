package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const loopbackTestHost = "127.0.0.1:47562"

func loopbackRequest(handler http.Handler, method, path, origin, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "http://"+loopbackTestHost+path, strings.NewReader(body))
	req.Host = loopbackTestHost
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	if method == http.MethodPost {
		req.Header.Set("Content-Type", "application/json")
	}
	if method == http.MethodOptions {
		req.Header.Set("Access-Control-Request-Method", http.MethodPost)
		req.Header.Set("Access-Control-Request-Private-Network", "true")
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	return response
}

func TestLoopbackChallengeBindsOriginHostAndSingleUseNonce(t *testing.T) {
	paired := make(chan [2]string, 1)
	handler := newLoopbackHandler(Config{Controllers: []string{"https://bootstrap.example"},
		DashboardOrigins: []string{"https://dashboard.example"}}, func(handle, origin string) error {
		paired <- [2]string{handle, origin}
		return nil
	})
	challenge := loopbackRequest(handler, http.MethodGet, "/v1/challenge", "https://dashboard.example", "")
	if challenge.Code != http.StatusOK || challenge.Header().Get("Access-Control-Allow-Origin") != "https://dashboard.example" {
		t.Fatalf("challenge was not allowed: %d %s", challenge.Code, challenge.Body.String())
	}
	var issued struct {
		Nonce string `json:"nonce"`
	}
	if err := json.Unmarshal(challenge.Body.Bytes(), &issued); err != nil || len(issued.Nonce) != 64 {
		t.Fatalf("bad nonce response: %#v, err=%v", issued, err)
	}
	if strings.Contains(challenge.Body.String(), "machine_id") {
		t.Fatal("loopback challenge must not expose stable machine identity")
	}
	body := `{"nonce":"` + issued.Nonce + `","handle":"0123456789abcdef0123456789abcdef"}`
	pairedResponse := loopbackRequest(handler, http.MethodPost, "/v1/pair", "https://dashboard.example", body)
	if pairedResponse.Code != http.StatusAccepted {
		t.Fatalf("pair was not accepted: %d %s", pairedResponse.Code, pairedResponse.Body.String())
	}
	select {
	case values := <-paired:
		if values[0] != "0123456789abcdef0123456789abcdef" || values[1] != "https://dashboard.example" {
			t.Fatalf("pair callback received wrong values: %#v", values)
		}
	case <-time.After(time.Second):
		t.Fatal("pair callback did not run")
	}
	replay := loopbackRequest(handler, http.MethodPost, "/v1/pair", "https://dashboard.example", body)
	if replay.Code != http.StatusConflict {
		t.Fatalf("replayed nonce returned %d", replay.Code)
	}
}

func TestLoopbackChallengeRejectsOriginHostAndExpiredNonce(t *testing.T) {
	now := time.Now()
	handler := newLoopbackHandlerWithClock(Config{Controllers: []string{"https://bootstrap.example"},
		DashboardOrigins: []string{"https://dashboard.example"}}, func(string, string) error { return nil },
		func() time.Time { return now })
	if got := loopbackRequest(handler, http.MethodGet, "/v1/challenge", "https://evil.example", "").Code; got != http.StatusForbidden {
		t.Fatalf("untrusted origin returned %d", got)
	}
	wrongHost := httptest.NewRequest(http.MethodGet, "http://evil.example/v1/challenge", nil)
	wrongHost.Header.Set("Origin", "https://dashboard.example")
	wrongHostResponse := httptest.NewRecorder()
	handler.ServeHTTP(wrongHostResponse, wrongHost)
	if wrongHostResponse.Code != http.StatusForbidden {
		t.Fatalf("rebinding Host returned %d", wrongHostResponse.Code)
	}
	challenge := loopbackRequest(handler, http.MethodGet, "/v1/challenge", "https://dashboard.example", "")
	var issued struct {
		Nonce string `json:"nonce"`
	}
	_ = json.Unmarshal(challenge.Body.Bytes(), &issued)
	now = now.Add(2 * time.Minute)
	body := `{"nonce":"` + issued.Nonce + `","handle":"0123456789abcdef0123456789abcdef"}`
	expired := loopbackRequest(handler, http.MethodPost, "/v1/pair", "https://dashboard.example", body)
	if expired.Code != http.StatusConflict {
		t.Fatalf("expired nonce returned %d", expired.Code)
	}
}

func TestLoopbackPreflightRequiresPinnedOriginAndPrivateNetworkPermission(t *testing.T) {
	handler := newLoopbackHandler(Config{Controllers: []string{"https://bootstrap.example"},
		DashboardOrigins: []string{"https://dashboard.example"}}, func(string, string) error { return nil })
	preflight := loopbackRequest(handler, http.MethodOptions, "/v1/pair", "https://dashboard.example", "")
	if preflight.Code != http.StatusNoContent || preflight.Header().Get("Access-Control-Allow-Private-Network") != "true" {
		t.Fatalf("private network preflight failed: %d %v", preflight.Code, preflight.Header())
	}
	challengePreflight := httptest.NewRequest(http.MethodOptions, "http://"+loopbackTestHost+challengePath, nil)
	challengePreflight.Host = loopbackTestHost
	challengePreflight.Header.Set("Origin", "https://dashboard.example")
	challengePreflight.Header.Set("Access-Control-Request-Method", http.MethodGet)
	challengePreflight.Header.Set("Access-Control-Request-Private-Network", "true")
	challengeResponse := httptest.NewRecorder()
	handler.ServeHTTP(challengeResponse, challengePreflight)
	if challengeResponse.Code != http.StatusNoContent {
		t.Fatalf("challenge PNA preflight returned %d", challengeResponse.Code)
	}
	missingPNA := httptest.NewRequest(http.MethodOptions, "http://"+loopbackTestHost+pairPath, nil)
	missingPNA.Host = loopbackTestHost
	missingPNA.Header.Set("Origin", "https://dashboard.example")
	missingPNA.Header.Set("Access-Control-Request-Method", http.MethodPost)
	missingPNAResponse := httptest.NewRecorder()
	handler.ServeHTTP(missingPNAResponse, missingPNA)
	if missingPNAResponse.Code != http.StatusForbidden {
		t.Fatalf("preflight without private-network request returned %d", missingPNAResponse.Code)
	}
	badBody := loopbackRequest(handler, http.MethodPost, "/v1/pair", "https://dashboard.example", `{"nonce":"x","handle":"bad"}`)
	if badBody.Code != http.StatusBadRequest {
		t.Fatalf("malformed pair body returned %d", badBody.Code)
	}
}
