#!/bin/bash
# Generic offload for final logs below /mnt/nvme/Monitoramento/.
set -uo pipefail

readonly SYNC_VERSION="1.1.3"
SOURCE_ROOT="${MONITORAMENTO_SYNC_SOURCE:-/mnt/nvme/Monitoramento/}"
DESTINATION="${MONITORAMENTO_SYNC_DESTINATION:-salte@office.salte.me:./}"
FILTER_FILE="${MONITORAMENTO_SYNC_FILTER_FILE:-/etc/monitoramento-sync/rsync_filter.conf}"
STATUS_FILE="${MONITORAMENTO_SYNC_STATUS_FILE:-/var/lib/monitoramento-sync/status.json}"
LOCK_FILE="${MONITORAMENTO_SYNC_LOCK_FILE:-/var/lib/monitoramento-sync/sync.lock}"
SSH_KEY="${MONITORAMENTO_SYNC_SSH_KEY:-/home/raspberrypi5/.ssh/monitoramento_sync}"
SSH_PORT="${MONITORAMENTO_SYNC_SSH_PORT:-2222}"
CONNECT_TIMEOUT="${MONITORAMENTO_SYNC_CONNECT_TIMEOUT:-10}"
RSYNC_TIMEOUT="${MONITORAMENTO_SYNC_TIMEOUT:-60}"
MAX_ATTEMPTS="${MONITORAMENTO_SYNC_MAX_ATTEMPTS:-3}"
RETRY_DELAY="${MONITORAMENTO_SYNC_RETRY_DELAY:-30}"
RSYNC_BIN="${MONITORAMENTO_SYNC_RSYNC_BIN:-rsync}"
SSH_BIN="${MONITORAMENTO_SYNC_SSH_BIN:-ssh}"
LOGGER_BIN="${MONITORAMENTO_SYNC_LOGGER_BIN:-logger}"

LAST_ATTEMPT=""
LAST_SUCCESS=""
DURATION=0
ELIGIBLE_FILES=0
FILES_TRANSFERRED=0
BYTES_TRANSFERRED=0
RSYNC_EXIT_CODE="null"
START_SECONDS=0
RSYNC_OUTPUT=""
STATUS_TEMP=""
ATTEMPTS=0

cleanup() {
    [[ -z "$RSYNC_OUTPUT" ]] || rm -f -- "$RSYNC_OUTPUT"
    [[ -z "$STATUS_TEMP" ]] || rm -f -- "$STATUS_TEMP"
}
trap cleanup EXIT

log_event() {
    local priority="$1"
    local message="$2"
    command -v "$LOGGER_BIN" >/dev/null 2>&1 || return 0
    "$LOGGER_BIN" -p "user.$priority" -t monitoramento-sync -- "$message" 2>/dev/null || true
}

utc_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

json_escape() {
    local value="$1"
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

read_last_success() {
    local line
    [[ -r "$STATUS_FILE" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ $line =~ \"last_success\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
            printf '%s' "${BASH_REMATCH[1]}"
            return 0
        fi
    done < "$STATUS_FILE"
}

write_status() {
    local destination_json success_json="null"
    destination_json=$(json_escape "$DESTINATION")
    [[ -z "$LAST_SUCCESS" ]] || success_json="\"$(json_escape "$LAST_SUCCESS")\""
    STATUS_TEMP=$(mktemp "$(dirname -- "$STATUS_FILE")/.status.XXXXXX") || return 1
    printf '{\n  "version": "%s",\n  "last_attempt": "%s",\n' \
        "$SYNC_VERSION" "$LAST_ATTEMPT" > "$STATUS_TEMP" || return 1
    printf '  "last_success": %s,\n  "duration": %d,\n' \
        "$success_json" "$DURATION" >> "$STATUS_TEMP" || return 1
    printf '  "eligible_files": %d,\n  "files_transferred": %d,\n' \
        "$ELIGIBLE_FILES" "$FILES_TRANSFERRED" >> "$STATUS_TEMP" || return 1
    printf '  "bytes_transferred": %d,\n  "attempts": %d,\n' \
        "$BYTES_TRANSFERRED" "$ATTEMPTS" >> "$STATUS_TEMP" || return 1
    printf '  "rsync_exit_code": %s,\n' \
        "$RSYNC_EXIT_CODE" >> "$STATUS_TEMP" || return 1
    printf '  "destination": "%s"\n}\n' "$destination_json" >> "$STATUS_TEMP" || return 1
    chmod 0640 "$STATUS_TEMP" && mv -f -- "$STATUS_TEMP" "$STATUS_FILE" || return 1
    STATUS_TEMP=""
}

complete_attempt() {
    local process_rc="$1"
    local result="$2"
    local priority=notice
    DURATION=$((SECONDS - START_SECONDS))
    [[ "$process_rc" -ne 0 ]] || LAST_SUCCESS=$(utc_now)
    [[ "$process_rc" -eq 0 ]] || priority=err
    if ! write_status; then
        log_event err "version=$SYNC_VERSION result=status_write_failed rc=$process_rc"
        return 74
    fi
    log_event "$priority" "version=$SYNC_VERSION result=$result rc=$process_rc attempts=$ATTEMPTS eligible=$ELIGIBLE_FILES transferred=$FILES_TRANSFERRED bytes=$BYTES_TRANSFERRED duration=$DURATION"
    return "$process_rc"
}

count_eligible_files() {
    find "$SOURCE_ROOT" \
        -type d \( -name '*.partial' -o -name '*.tmp' -o -name '.rsync-partial' -o -name quarantine \) -prune -o \
        -type f -path '*/logs/*' \
        ! -name '*.partial' ! -name '*.partial.avi' ! -name '*.tmp' \
        ! -path "${SOURCE_ROOT}DrowsyDriving/logs/eventos.csv" -printf '.\n' | wc -l
}

validate_inputs() {
    [[ -d "$SOURCE_ROOT" ]] || return 3
    [[ -r "$FILTER_FILE" ]] || return 4
    [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || return 5
    [[ "$CONNECT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || return 5
    [[ "$RSYNC_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || return 5
    [[ "$MAX_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || return 5
    [[ "$RETRY_DELAY" =~ ^[0-9]+$ ]] || return 5
    command -v "$RSYNC_BIN" >/dev/null 2>&1 || return 127
}

collect_transfer_stats() {
    local marker item length ignored
    while IFS='|' read -r marker item length ignored; do
        [[ "$marker" == '@@MONITORAMENTO@@' ]] || continue
        [[ "${item:0:2}" == '>f' && "$length" =~ ^[0-9]+$ ]] || continue
        FILES_TRANSFERRED=$((FILES_TRANSFERRED + 1))
        BYTES_TRANSFERRED=$((BYTES_TRANSFERRED + length))
    done < "$RSYNC_OUTPUT"
}

run_rsync() {
    local ssh_command
    local -a ssh_args rsync_args
    ssh_args=("$SSH_BIN" -p "$SSH_PORT" -i "$SSH_KEY" -o BatchMode=yes
        -o StrictHostKeyChecking=accept-new -o "ConnectTimeout=$CONNECT_TIMEOUT")
    printf -v ssh_command '%q ' "${ssh_args[@]}"
    rsync_args=(--archive --ignore-existing --delay-updates --prune-empty-dirs
        "--filter=merge $FILTER_FILE"
        --partial-dir=.rsync-partial "--timeout=$RSYNC_TIMEOUT" --itemize-changes
        '--out-format=@@MONITORAMENTO@@|%i|%l|%n' "--rsh=${ssh_command% }")
    "$RSYNC_BIN" "${rsync_args[@]}" -- "$SOURCE_ROOT" "$DESTINATION" > "$RSYNC_OUTPUT" 2>&1
}

run_with_retry() {
    local attempt rc=1
    for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
        ATTEMPTS=$attempt
        : > "$RSYNC_OUTPUT"
        run_rsync
        rc=$?
        [[ "$rc" -ne 0 ]] || return 0
        [[ "$attempt" -ge "$MAX_ATTEMPTS" ]] || sleep "$RETRY_DELAY"
    done
    return "$rc"
}

prepare_attempt() {
    local status_dir lock_dir
    status_dir=$(dirname -- "$STATUS_FILE")
    lock_dir=$(dirname -- "$LOCK_FILE")
    mkdir -p -- "$status_dir" "$lock_dir" || return 73
    START_SECONDS=$SECONDS
    LAST_ATTEMPT=$(utc_now)
    LAST_SUCCESS=$(read_last_success)
    command -v flock >/dev/null 2>&1 || return 127
    exec 9>"$LOCK_FILE" || return 73
    flock -n 9 || return 75
}

main() {
    local rc
    SOURCE_ROOT="${SOURCE_ROOT%/}/"
    prepare_attempt
    rc=$?
    if [[ "$rc" -eq 75 ]]; then
        log_event notice "version=$SYNC_VERSION result=lock_busy"
        return 0
    fi
    [[ "$rc" -eq 0 ]] || { complete_attempt "$rc" preflight_failed; return $?; }
    validate_inputs
    rc=$?
    [[ "$rc" -eq 0 ]] || { complete_attempt "$rc" invalid_configuration; return $?; }
    ELIGIBLE_FILES=$(count_eligible_files)
    rc=$?
    [[ "$rc" -eq 0 ]] || { complete_attempt "$rc" eligibility_failed; return $?; }
    RSYNC_OUTPUT=$(mktemp "$(dirname -- "$STATUS_FILE")/.rsync-output.XXXXXX") || {
        complete_attempt 74 output_capture_failed
        return $?
    }
    run_with_retry
    RSYNC_EXIT_CODE=$?
    collect_transfer_stats
    [[ "$RSYNC_EXIT_CODE" -ne 0 ]] || { complete_attempt 0 success; return $?; }
    complete_attempt "$RSYNC_EXIT_CODE" transfer_failed
}

main "$@"
