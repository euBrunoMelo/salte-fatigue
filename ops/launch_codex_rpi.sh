#!/usr/bin/env bash
# Run in a normal Ubuntu terminal. Select the LAN or VPN route explicitly.
set -euo pipefail

usage() {
    printf 'Uso: %s --lan|--vpn --check|--run\n' "$0" >&2
    exit 2
}
[[ $# -eq 2 ]] || usage
case $1 in
    --lan) connection_mode=lan ;;
    --vpn) connection_mode=vpn ;;
    *) usage ;;
esac
case $2 in
    --check|--run) mode=$2 ;;
    *) usage ;;
esac

VPN_IP=100.120.192.2
RPI_USER=raspberrypi5
KEY="$HOME/.ssh/id_ed25519_codex_rpi_sala"
VPN_CFG="$HOME/.config/ai-jail/rpi-sala"
LAN_CFG="$HOME/.config/ai-jail/rpi-sala-lan"
if [[ $connection_mode == lan ]]; then
    CFG=$LAN_CFG
else
    CFG=$VPN_CFG
fi
AM_BIN="$HOME/.cache/ai-memory/native-runner/ai-memory"
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)

die() {
    printf 'Erro: %s\n' "$*" >&2
    exit 1
}

for tool in ai-jail bwrap codex ip ssh ssh-agent ssh-add; do
    command -v "$tool" >/dev/null || die "Comando ausente: $tool"
done
[[ $(stat -c %u "$(command -v bwrap)") == 0 ]] ||
    die 'Execute no terminal normal do PC, fora de outra jail/container.'
[[ -f $KEY && -f $KEY.pub && -f $CFG/config && -f $CFG/identity.pub && -f $CFG/known_hosts ]] ||
    die "Preparação SSH incompleta para $connection_mode. Consulte docs/CODEX_RPI_SSH_UBUNTU.md."
cmp -s "$KEY.pub" "$CFG/identity.pub" ||
    die 'A chave pública compartilhada difere da chave exclusiva no PC.'
ssh_effective=$(ssh -G -T -F "$CFG/config" rpi-sala) ||
    die 'A configuração SSH selecionada é inválida.'
RPI_IP=$(awk '$1 == "hostname" { print $2; exit }' <<< "$ssh_effective")
configured_user=$(awk '$1 == "user" { print $2; exit }' <<< "$ssh_effective")
configured_port=$(awk '$1 == "port" { print $2; exit }' <<< "$ssh_effective")
[[ -n $RPI_IP && $configured_user == "$RPI_USER" && $configured_port == 22 ]] ||
    die 'HostName, User ou Port inválido na configuração SSH selecionada.'
route=$(ip -4 route get "$RPI_IP") || die "Sem rota IPv4 para $RPI_IP."
if [[ $connection_mode == vpn ]]; then
    [[ $RPI_IP == "$VPN_IP" && " $route " == *' dev netmaker '* ]] ||
        die "A rota para $RPI_IP não passa pela interface netmaker."
else
    [[ $RPI_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
        die 'O HostName da configuração LAN deve ser um endereço IPv4.'
    route_iface=$(awk '{ for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }' <<< "$route")
    [[ -n $route_iface && $route != *' via '* &&
       ( -e /sys/class/net/$route_iface/phy80211 || -d /sys/class/net/$route_iface/wireless ) ]] ||
        die "A rota para $RPI_IP não é direta pelo Wi-Fi; não usarei a VPN como alternativa."
fi
[[ -x $AM_BIN ]] ||
    die "Cliente nativo do ai-memory ausente: $AM_BIN (execute ai-memory run --help fora da jail)."
[[ -f $HOME/.local/bin/ai-memory && -e $HOME/.local/npm/bin/codex ]] ||
    die 'Os caminhos locais de ai-memory/Codex mudaram; ajuste os mapeamentos antes de continuar.'
[[ -d $HOME/.codex && -d $HOME/.local/share/ai-memory ]] ||
    die 'Estado do Codex ou dados do ai-memory ausentes no PC.'

printf 'SSH para %s@%s pelo modo %s.\n' "$RPI_USER" "$RPI_IP" "$connection_mode"
cd -- "$PROJECT_DIR"
(
    set -euo pipefail
    unset SSH_AUTH_SOCK SSH_AGENT_PID
    agent_env=$(ssh-agent -s)
    eval "$agent_env" >/dev/null
    trap 'ssh-agent -k >/dev/null 2>&1 || true' EXIT

    ssh-add -t 6h -H "$CFG/known_hosts" -h "$RPI_USER@$RPI_IP" "$KEY"
    ssh-add -l
    [[ $(ssh-add -L | wc -l) -eq 1 ]] || die 'O agente dedicado deve conter somente uma chave.'

    jail_args=(
        --clean
        --private-home
        --no-ssh
        --no-agent-state
        --no-docker
        --no-systemd-user
        --no-inherit-env
        --no-save-config
        --network
        --map "$CFG:/tmp/codex-rpi-ssh"
        --rw-map "$SSH_AUTH_SOCK:/tmp/codex-rpi-agent.sock"
        --env SSH_AUTH_SOCK=/tmp/codex-rpi-agent.sock
        --map "$HOME/.local/bin/ai-memory"
        --map "$AM_BIN:/tmp/ai-memory-native"
        --env AI_MEMORY_NATIVE_BIN=/tmp/ai-memory-native
        --map "$HOME/.local/npm"
        --rw-map "$HOME/.codex"
        --rw-map "$HOME/.local/share/ai-memory"
    )
    codex_args=(
        "$HOME/.local/bin/ai-memory" run --no-autowire
        --executable "$HOME/.local/npm/bin/codex" codex
        --no-daemon
        --sandbox danger-full-access
        --ask-for-approval on-request
    )

    if [[ $mode == --check ]]; then
        ai-jail "${jail_args[@]}" --dry-run -- "${codex_args[@]}"
        printf '\nRevise os mapeamentos. Não deve haver ~/.ssh real, $HOME inteiro ou outro socket SSH.\n'
        printf 'Depois execute: bash ops/launch_codex_rpi.sh --%s --run\n' "$connection_mode"
    else
        ai-jail "${jail_args[@]}" -- "${codex_args[@]}"
    fi
)
