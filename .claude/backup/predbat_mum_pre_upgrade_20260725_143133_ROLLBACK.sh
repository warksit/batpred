#!/bin/bash
# Rollback mum Predbat to pre-upgrade snapshot 20260725_143133
set -euo pipefail
BACKUP_TGZ="/Users/home/Documents/code/batpred/.claude/backup/predbat_mum_pre_upgrade_20260725_143133.tar.gz"
HOST="hassio@100.110.70.80"
test -f "$BACKUP_TGZ"
echo "Stopping Predbat addon..."
ssh -o BatchMode=yes "$HOST" 'sudo -n docker stop addon_6adb4f0d_predbat'
echo "Uploading backup..."
ssh -o BatchMode=yes "$HOST" 'cat > /tmp/predbat_rollback_20260725_143133.tar.gz' < "$BACKUP_TGZ"
echo "Restoring full /addon_configs/6adb4f0d_predbat ..."
ssh -o BatchMode=yes "$HOST" 'set -e
  sudo -n rm -rf /addon_configs/6adb4f0d_predbat
  sudo -n tar xzf /tmp/predbat_rollback_20260725_143133.tar.gz -C /addon_configs
  sudo -n docker start addon_6adb4f0d_predbat
  sleep 5
  sudo -n docker ps --filter name=addon_6adb4f0d_predbat --format "{{.Names}} {{.Status}}"
  sudo -n grep THIS_VERSION /addon_configs/6adb4f0d_predbat/predbat.py | head -1
'
echo "Rollback complete."
