#!/usr/bin/env bash
# Copy the client document tree from SharePoint to Azure Blob (xplan-documents), server to
# server - nothing goes through anyone's PC. Run inside tmux so a dropped SSH session
# doesn't stop it:   tmux new -s docs   then   bash docs_to_blob.sh
#
# Needs two rclone remotes (see README / chat steps):
#   sp: SharePoint (onedrive type, the team site's Documents library)
#   az: Azure Blob (azureblob type, SAS limited to add/create/write/list on xplan-documents)
# Safe to re-run: files already copied (same size and date) are skipped. Never deletes.
set -euo pipefail

SRC="${SRC:-sp:General/XPlan Files}"
DEST="${DEST:-az:xplan-documents/2026-10-02_1612/XPlan Files}"
LOG="${LOG:-$HOME/docs-copy-$(date +%Y%m%d-%H%M).log}"

for r in sp az; do
  rclone listremotes | grep -qx "$r:" || { echo "rclone remote '$r:' is missing - set it up first."; exit 1; }
done

echo "==> Source size (this lists every file, a few minutes)"
rclone size "$SRC"

echo "==> Copying $SRC  ->  $DEST"
echo "    Log: $LOG"
rclone copy "$SRC" "$DEST" --exclude "Xplan Archive/**" \
  --transfers 8 --checkers 16 \
  --azureblob-no-check-container \
  --retries 5 --low-level-retries 20 \
  --stats 30s --stats-one-line -P \
  --log-file "$LOG" --log-level INFO

echo "==> Checking every file arrived (names and sizes)"
if rclone check "$SRC" "$DEST" --exclude "Xplan Archive/**" --size-only --one-way \
     --missing-on-dst "$HOME/docs-missing.txt" --differ "$HOME/docs-differ.txt" \
     --log-file "$LOG" --log-level NOTICE; then
  echo "All files are in Azure with matching sizes."
else
  echo "Some files are missing or differ - counts are above; lists in ~/docs-missing.txt"
  echo "and ~/docs-differ.txt (these contain client file names; don't paste them anywhere)."
  echo "Run this script again to copy what's missing."
  exit 1
fi
