#!/usr/bin/env bash
# Build the Terminal MCP Bootstrap helper for Windows.
#
# UNSIGNED. There is no Authenticode certificate in this project, so the
# artifact this produces is a DEVELOPER build: SmartScreen will warn, and
# the Dashboard says so rather than coaching anyone to click past it.
# When a certificate exists, sign here and pass -X main.Signed=true --
# that flag defaults to false precisely so an unsigned build can never
# report itself as signed.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${VERSION:-0.1.0-dev}"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
OUT="${OUT:-dist/terminal-mcp-bootstrap.exe}"
mkdir -p "$(dirname "$OUT")"
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build \
  -ldflags "-s -w -X main.Version=${VERSION} -X main.BuildSHA=${SHA} -X main.Signed=false" \
  -o "$OUT" ./cmd/terminal-mcp-bootstrap
echo "built $OUT"
sha256sum "$OUT"
