#!/bin/bash
# Install only the versioned generic sync assets. Activation remains manual.
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REQUIRED_ASSETS=(
    VERSION
    monitoramento-sync.sh
    monitoramento-sync.service
    monitoramento-sync.timer
    99-monitoramento-sync
    rsync_filter.conf
    wifi-manager-logrotate
)

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this installer as root.\n' >&2
    exit 1
fi

for asset in "${REQUIRED_ASSETS[@]}"; do
    if [[ ! -f "$SCRIPT_DIR/$asset" ]]; then
        printf 'Missing required asset: %s\n' "$SCRIPT_DIR/$asset" >&2
        exit 1
    fi
done

install -d -o root -g root -m 0755 /etc/monitoramento-sync
install -d -o root -g root -m 0755 /usr/share/monitoramento-sync
install -o root -g root -m 0755 "$SCRIPT_DIR/monitoramento-sync.sh" /usr/local/sbin/monitoramento-sync.sh
install -o root -g root -m 0644 "$SCRIPT_DIR/rsync_filter.conf" /etc/monitoramento-sync/rsync_filter.conf
install -o root -g root -m 0644 "$SCRIPT_DIR/VERSION" /usr/share/monitoramento-sync/VERSION
install -o root -g root -m 0644 "$SCRIPT_DIR/monitoramento-sync.service" /etc/systemd/system/monitoramento-sync.service
install -o root -g root -m 0644 "$SCRIPT_DIR/monitoramento-sync.timer" /etc/systemd/system/monitoramento-sync.timer
install -o root -g root -m 0755 "$SCRIPT_DIR/99-monitoramento-sync" /etc/NetworkManager/dispatcher.d/99-monitoramento-sync
install -o root -g root -m 0644 "$SCRIPT_DIR/wifi-manager-logrotate" /etc/logrotate.d/wifi-manager

printf 'Monitoramento sync assets installed; no unit was enabled, disabled, started, or stopped.\n'
