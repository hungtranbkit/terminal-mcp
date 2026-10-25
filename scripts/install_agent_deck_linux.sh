#!/usr/bin/env bash
# Pinned, checksum-verified optional Linux Agent Deck install.
# Does not run vendor install.sh, change tmux config, create sessions or use sudo.
set -euo pipefail

version="1.16.30"
base="https://github.com/asheshgoplani/agent-deck/releases/download/v${version}"
[[ "$(uname -s)" == Linux ]] || { echo "Linux only" >&2; exit 2; }
case "$(uname -m)" in
  x86_64|amd64)
    arch=amd64
    hash=88d9629488f17734057a7dbf9fcdf640ffb23311bf9e2ebf4d304123ff8b25bc
    ;;
  aarch64|arm64)
    arch=arm64
    hash=88a9b4d1ccecbd7081ffd63d62986781553eb1c025e25d7eec2bbc7d0d667027
    ;;
  *) echo "Unsupported Linux architecture" >&2; exit 2 ;;
esac

for tool in curl sha256sum tar install; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 2; }
done
dest="${HOME}/.local/bin/agent-deck"
if [[ -e "$dest" ]]; then
  if "$dest" --version 2>/dev/null | grep -Fq "Agent Deck v${version}"; then
    echo "Already installed: $dest"
    "$dest" --version
    exit 0
  fi
  echo "Existing binary found at $dest; refusing to overwrite" >&2
  exit 3
fi

tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
file="$tmp/agent-deck.tar.gz"
curl --fail --location --silent --show-error --retry 2 --max-time 120 \
  --output "$file" "$base/agent-deck_${version}_linux_${arch}.tar.gz"
printf '%s  %s\n' "$hash" "$file" | sha256sum --check --status || {
  echo "Checksum mismatch: refusing install" >&2
  exit 4
}
tar -xzf "$file" -C "$tmp" agent-deck
mkdir -p "${HOME}/.local/bin"
install -m 0755 "$tmp/agent-deck" "$dest"
"$dest" --version
echo "Installed $dest (no tmux changes; run interactively when desired)"
