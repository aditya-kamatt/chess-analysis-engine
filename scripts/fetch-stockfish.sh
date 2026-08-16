#!/usr/bin/env bash
# Fetch a pinned Stockfish build into vendor/.
set -euo pipefail

VERSION="${STOCKFISH_VERSION:-sf_18}"
BUILD="${STOCKFISH_BUILD:-x86-64-bmi2}"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$root/vendor"
binary="$dest/stockfish"

url="https://github.com/official-stockfish/Stockfish/releases/download/${VERSION}/stockfish-ubuntu-${BUILD}.tar"

mkdir -p "$dest"
echo "fetching stockfish ${VERSION} (${BUILD})"
curl -sSfL --max-time 300 "$url" | tar -x -C "$dest" --strip-components=1 -f -

# The tarball ships the binary under its build name; normalise it.
if [[ ! -x "$binary" ]]; then
  mv "$dest/stockfish-ubuntu-${BUILD}" "$binary"
fi
chmod +x "$binary"

echo -n "installed: "
"$binary" --help 2>/dev/null | head -1 || true
printf 'uci\nquit\n' | "$binary" | grep -m1 '^id name'