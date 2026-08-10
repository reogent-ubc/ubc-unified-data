#!/usr/bin/env bash
set -euo pipefail

SRC_REPO="https://github.com/DonneyF/ubc-pair-grade-data.git"
DEST="$(cd "$(dirname "$0")/.." && pwd)/data/grades/raw"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth 1 --filter=blob:none --sparse "$SRC_REPO" "$TMP" >/dev/null 2>&1
(
  cd "$TMP"
  git sparse-checkout set tableau-dashboard-v2/UBCV
)

rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$TMP/tableau-dashboard-v2/UBCV/." "$DEST"
echo "Synced grade distributions to $DEST"