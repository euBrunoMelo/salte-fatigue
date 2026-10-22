#!/usr/bin/env bash
# F14: check syntax without executing scripts. Usage: bash ops/validate_shell.sh [FILE ...]
set -uo pipefail

declare -a shell_validation_scripts=("$@")
declare -i shell_validation_status=0

if (( $# == 0 )); then
    # F14: CDPATH must not print a directory into the captured root.
    shell_validation_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P) || exit 1
    # An explicit list also detects missing required scripts, unlike a glob.
    shell_validation_scripts=(
        "$shell_validation_root/ops/launch_codex_rpi.sh"
        "$shell_validation_root/ops/setup_codex_rpi_lan.sh"
        "$shell_validation_root/ops/setup_codex_rpi_ssh.sh"
        "$shell_validation_root/ops/validate_shell.sh"
        "$shell_validation_root/sync/install-monitoramento-sync.sh"
        "$shell_validation_root/sync/monitoramento-sync.sh"
        "$shell_validation_root/sync/99-monitoramento-sync"
    )
fi

for shell_validation_script in "${shell_validation_scripts[@]}"; do
    if [[ ! -f $shell_validation_script ]]; then
        printf 'Arquivo shell inválido: %s (esperado: arquivo regular existente).\n' "$shell_validation_script" >&2
        shell_validation_status=1
        continue
    fi
    if ! bash -n -- "$shell_validation_script"; then
        shell_validation_status=1
    fi
done

exit "$shell_validation_status"
