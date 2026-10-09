#!/usr/bin/env bash
# Nightly: dump the database, check the dump can be read back, encrypt it to the age public key
# and upload it to Azure (backups/db/YYYY/MM/brightly-YYYYMMDD-HHMM.dump.age). The server only
# holds the PUBLIC key, so a stolen server can't read old backups. Retention is an Azure
# lifecycle rule on the container (keep 35 daily, then monthly for 7 years).
#   backup.sh loop   wait for BACKUP_HOUR each day (container default)
#   backup.sh now    one backup immediately
set -euo pipefail

backup_vault() {
  # Vault files are already encrypted (VAULT_KEY); the archive is encrypted again to the backup
  # key. snapshot/vault.db is the vault's own consistent copy of its database (made hourly).
  [ -d /vault/files ] || return 0
  local stamp="$1" file="/spool/vault-$1.tar.age"
  tar -C /vault -cf - files snapshot | age -r "$BACKUP_AGE_RECIPIENT" -o "$file"
  sha256sum "$file" | awk '{print $1}' > "${file}.sha256"
  rclone copy "/spool" ":azureblob,sas_url='${AZURE_BACKUP_SAS_URL}':backups/vault/$(date +%Y/%m)" \
    --include "vault-${stamp}.*" --azureblob-no-check-container --retries 5
}

run_backup() {
  local stamp file
  stamp=$(date +%Y%m%d-%H%M)
  file="/spool/brightly-${stamp}.dump"
  echo "[$(date -Is)] dumping"
  pg_dump --format=custom --compress=6 --no-owner --file "$file"
  pg_restore --list "$file" > /dev/null          # proves the dump is complete and readable
  age -r "$BACKUP_AGE_RECIPIENT" -o "${file}.age" "$file"
  rm -f "$file"
  sha256sum "${file}.age" | awk '{print $1}' > "${file}.age.sha256"
  echo "[$(date -Is)] uploading $(du -h "${file}.age" | cut -f1)"
  rclone copy "/spool" ":azureblob,sas_url='${AZURE_BACKUP_SAS_URL}':backups/db/$(date +%Y/%m)" \
    --include "brightly-${stamp}.*" --azureblob-no-check-container --retries 5
  backup_vault "$stamp"
  find /spool -name 'brightly-*' -mtime +3 -delete
  find /spool -name 'vault-*' -mtime +3 -delete   # keep 3 days locally for a quick restore
  date -Is > /spool/LAST_SUCCESS                      # monitoring alerts if this gets old
  echo "[$(date -Is)] backup ok: brightly-${stamp}.dump.age"
}

case "${1:-loop}" in
  now) run_backup ;;
  loop)
    while true; do
      now=$(date +%s)
      next=$(date -d "today ${BACKUP_HOUR}:00" +%s)
      [ "$next" -le "$now" ] && next=$(date -d "tomorrow ${BACKUP_HOUR}:00" +%s)
      sleep $((next - now))
      # a separate process, so `set -e` stops the backup at the first failing step
      "$0" now || echo "[$(date -Is)] BACKUP FAILED"   # LAST_SUCCESS goes stale; monitoring alerts
    done ;;
  *) echo "usage: backup.sh [now|loop]"; exit 2 ;;
esac
