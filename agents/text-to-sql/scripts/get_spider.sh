#!/usr/bin/env bash
# Download Spider from its official source and rebuild the exact splits by example id.
#   scripts/get_spider.sh                 # downloads, unpacks to ./spider_data, builds data/*.jsonl
#   SPIDER_ZIP=/path/spider.zip scripts/get_spider.sh   # use a zip you already downloaded
#   SPIDER_DIR=/path/to/spider_data scripts/get_spider.sh   # use an unpacked copy, skip download
# Spider (Yale LILY lab, https://yale-lily.github.io/spider) is CC BY-SA 4.0; see NOTICE.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPIDER_DIR="${SPIDER_DIR:-$ROOT/spider_data}"
# Official link published on the Spider page (Google Drive).
SPIDER_GDRIVE_ID="1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J"

if [ ! -f "$SPIDER_DIR/dev.json" ]; then
  ZIP="${SPIDER_ZIP:-$ROOT/spider.zip}"
  if [ ! -f "$ZIP" ]; then
    command -v gdown >/dev/null || { echo "install gdown (pip install gdown) or download spider.zip from https://yale-lily.github.io/spider and set SPIDER_ZIP"; exit 1; }
    gdown "$SPIDER_GDRIVE_ID" -O "$ZIP"
  fi
  TMP="$(mktemp -d)"; unzip -q "$ZIP" -d "$TMP"
  SRC="$(dirname "$(find "$TMP" -name dev.json | head -1)")"
  mkdir -p "$SPIDER_DIR"; cp -r "$SRC"/. "$SPIDER_DIR"/; rm -rf "$TMP"
fi

SPIDER_DIR="$SPIDER_DIR" python3 "$ROOT/scripts/build_splits.py"

# Verify against the pre-registered hashes.
cd "$ROOT"
echo "dc5481988bbd4541b6490a87e1fdedde9cb21dfd8333ab541872e4b1403e62c9  data/heldout.jsonl" | sha256sum -c -
