#!/usr/bin/env bash
# Fetch a pinned Stockfish build into vendor/.
set -euo pipefail

VERSION="${STOCKFISH_VERSION:-sf_18}"
BUILD="${STOCKFISH_BUILD:-x86-64-bmi2}"
SHA256="${STOCKFISH_SHA256:-}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$root/vendor"
binary="$dest/stockfish"

url="https://github.com/official-stockfish/Stockfish/releases/download/${VERSION}/stockfish-ubuntu-${BUILD}.tar"

mkdir -p "$dest"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT

echo "fetching stockfish ${VERSION} (${BUILD})"
curl -sSfL --max-time 300 "$url" -o "$archive"

if [[ -n "$SHA256" ]]; then
  echo "verifying Stockfish archive checksum"
  expected="${SHA256,,}"
  actual="$(sha256sum "$archive" | awk '{print tolower($1)}')"
  [[ "$actual" == "$expected" ]] || {
    echo "Stockfish checksum mismatch" >&2
    exit 1
  }
fi

tar -x -C "$dest" --strip-components=1 -f "$archive"

# The tarball ships the binary under its build name; normalise it.
if [[ ! -x "$binary" ]]; then
  mv "$dest/stockfish-ubuntu-${BUILD}" "$binary"
fi
chmod +x "$binary"

echo -n "installed: "
"$binary" --help 2>/dev/null | head -1 || true
printf 'uci\nquit\n' | "$binary" | grep -m1 '^id name'