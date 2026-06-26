#!/usr/bin/env bash
# Download woff2 files for Overpass, Lato, JetBrains Mono into assets/fonts/.
# This is the ONLY network step; resulting files are then bundled with the
# container or zipped with the project.
#
# Usage:  bash scripts/fetch-fonts.sh [DEST_DIR]
# Or, automatically during:  docker build .
#
# Strategy: fetch each family's CSS from Google Fonts CSS2 with a modern
# User-Agent (gets woff2), parse out the src URLs with grep, then download
# each .woff2 by weight (assumes weights are returned in the requested
# order). Robust to Google rotating their /s/... paths.

set -euo pipefail

DEST="${1:-assets/fonts}"
mkdir -p "$DEST"

UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# family-slug:weight-list
FAMILIES=(
  "overpass:Overpass:300,400,600,700,800,900"
  "lato:Lato:300,400,700,900"
  "jetbrains-mono:JetBrains+Mono:400,500,700"
)

fetch_family() {
  local slug="$1"
  local family="$2"
  local weights="$3"
  local url="https://fonts.googleapis.com/css2?family=${family}:wght@$(echo "$weights" | tr ',' ';')&display=swap"

  local css
  css="$(curl -fsSL -H "User-Agent: $UA" "$url")"

  # Extract woff2 URLs in the order they appear (matches weight order in our request)
  local urls
  mapfile -t urls < <(echo "$css" | grep -oE 'https://[^)]*\.woff2')

  # Map weight order: Google returns blocks in the order requested
  IFS=',' read -ra weight_arr <<< "$weights"
  if [ "${#urls[@]}" -lt "${#weight_arr[@]}" ]; then
    echo "ERROR: $family — expected ${#weight_arr[@]} weights, got ${#urls[@]} URLs" >&2
    return 1
  fi

  for i in "${!weight_arr[@]}"; do
    local w="${weight_arr[$i]}"
    local out="$DEST/${slug}-${w}.woff2"
    if [ -f "$out" ] && [ -s "$out" ]; then
      echo "✓ $out (cached)"
      continue
    fi
    echo "↓ $out"
    curl -fsSL -H "User-Agent: $UA" -o "$out" "${urls[$i]}"
  done
}

for spec in "${FAMILIES[@]}"; do
  IFS=':' read -r slug family weights <<< "$spec"
  fetch_family "$slug" "$family" "$weights"
done

echo ""
echo "✓ Fonts downloaded to $DEST/"
echo "  ($(ls "$DEST" | wc -l | tr -d ' ') files, $(du -sh "$DEST" | cut -f1))"
