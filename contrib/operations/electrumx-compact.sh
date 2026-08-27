#!/bin/bash
# Compact the ElectrumX history DB, resetting the uint16 flush counter
# (hard ceiling 65,535; ~1,440 flushes/day at PIVX 60s blocks = ~45 days
# of headroom per compaction). Crash signature when it overflows:
#   struct.error: 'H' format requires 0 <= number <= 65535
# Deploy to /usr/local/sbin/ and drive it with a monthly systemd timer
# (see docs/pivx-sapling.rst, Operator Notes). Stagger multi-node
# deployments so at least one node stays up.
set -euo pipefail
log() { echo "$(date -Is) $*"; }

if [ -x /root/electrumx/venv/bin/python ]; then
    PY=/root/electrumx/venv/bin/python
else
    PY=/root/.pyenv/versions/3.13.1/bin/python
fi

set -a; source <(sed 's/ *= */=/' /etc/pivx.conf); set +a

log "stopping electrumx for history compaction"
systemctl stop electrumx
trap 'log "starting electrumx"; systemctl start electrumx' EXIT
"$PY" /root/electrumx/electrumx_compact_history
log "compaction complete"
