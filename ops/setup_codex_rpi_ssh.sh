#!/usr/bin/env bash
# Run once in a normal Ubuntu terminal, outside ai-jail.
set -euo pipefail

RPI_IP=100.120.192.2
RPI_USER=raspberrypi5
KEY="$HOME/.ssh/id_ed25519_codex_rpi_sala"
CFG="$HOME/.config/ai-jail/rpi-sala"

die() {
    printf 'Erro: %s\n' "$*" >&2
    exit 1
}

for tool in ai-jail bwrap codex ip ssh ssh-keygen ssh-keyscan ssh-copy-id; do
    command -v "$tool" >/dev/null || die "Comando ausente: $tool"
done
[[ $(stat -c %u "$(command -v bwrap)") == 0 ]] ||
    die 'Execute no terminal normal do PC, fora de outra jail/container.'
[[ " $(ip route get "$RPI_IP") " == *' dev netmaker '* ]] ||
    die "A rota para $RPI_IP não passa pela interface netmaker."

ai-jail --clean --no-save-config --dry-run -- codex --help >/dev/null ||
    die 'ai-jail/bubblewrap não iniciou. Corrija isso no PC antes de continuar.'

install -d -m 700 "$HOME/.ssh" "$CFG"
if [[ ! -e $KEY && ! -e $KEY.pub ]]; then
    printf 'Crie uma senha para a chave exclusiva do Raspberry.\n'
    ssh-keygen -t ed25519 -a 64 -f "$KEY" -C codex-rpi-sala
elif [[ ! -f $KEY || ! -f $KEY.pub ]]; then
    die "Existe apenas parte do par de chaves em $KEY; confira antes de continuar."
else
    printf 'Chave existente preservada: %s\n' "$KEY"
fi
chmod 600 "$KEY"
chmod 644 "$KEY.pub"

# Fetch the host key through the already trusted SSH path, then compare it to
# the key currently served on the VPN before recording a dedicated known_hosts.
admin_options=(-o StrictHostKeyChecking=yes)
if [[ -n ${RPI_ADMIN_KEY:-} ]]; then
    [[ -f $RPI_ADMIN_KEY ]] || die "Chave administrativa ausente: $RPI_ADMIN_KEY"
    admin_options+=(-o "IdentityFile=$RPI_ADMIN_KEY" -o IdentitiesOnly=yes)
fi
trusted_key=$(
    ssh "${admin_options[@]}" "$RPI_USER@$RPI_IP" \
        'cat /etc/ssh/ssh_host_ed25519_key.pub' |
        awk '$1 == "ssh-ed25519" { print $1 " " $2; exit }'
)
[[ -n $trusted_key ]] || die 'Não foi possível ler a chave do servidor por SSH já confiável.'
vpn_key=$(
    ssh-keyscan -T 5 -t ed25519 "$RPI_IP" 2>/dev/null |
        awk '$2 == "ssh-ed25519" { print $2 " " $3; exit }'
)
[[ -n $vpn_key && $vpn_key == "$trusted_key" ]] ||
    die 'A chave apresentada na VPN difere da obtida pela conexão confiável.'

known_hosts_tmp=$(mktemp "$CFG/.known_hosts.XXXXXX")
config_tmp=$(mktemp "$CFG/.config.XXXXXX")
trap 'rm -f -- "$known_hosts_tmp" "$config_tmp"' EXIT
printf '%s %s\n' "$RPI_IP" "$trusted_key" > "$known_hosts_tmp"
chmod 600 "$known_hosts_tmp"
mv -f -- "$known_hosts_tmp" "$CFG/known_hosts"

printf 'Identidade do Raspberry validada: '
ssh-keygen -lf "$CFG/known_hosts"

copy_options=(
    -o StrictHostKeyChecking=yes
    -o "UserKnownHostsFile=$CFG/known_hosts"
    -o GlobalKnownHostsFile=/dev/null
    -o HostKeyAlgorithms=ssh-ed25519
)
if [[ -n ${RPI_ADMIN_KEY:-} ]]; then
    copy_options+=(-o "IdentityFile=$RPI_ADMIN_KEY" -o IdentitiesOnly=yes)
fi
ssh-copy-id -i "$KEY.pub" "${copy_options[@]}" "$RPI_USER@$RPI_IP"

# Ignore the desktop SSH agent and all desktop SSH configuration for this test.
ssh -F /dev/null \
    -o IdentityAgent=none \
    -o IdentitiesOnly=yes \
    -o HostKeyAlgorithms=ssh-ed25519 \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$CFG/known_hosts" \
    -o StrictHostKeyChecking=yes \
    -i "$KEY" \
    "$RPI_USER@$RPI_IP" 'hostname; id -un'

install -m 644 "$KEY.pub" "$CFG/identity.pub"
cat > "$config_tmp" <<EOF
Host rpi-sala
    HostName $RPI_IP
    User $RPI_USER
    Port 22

    IdentityAgent /tmp/codex-rpi-agent.sock
    IdentityFile /tmp/codex-rpi-ssh/identity.pub
    IdentitiesOnly yes
    PreferredAuthentications publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no

    UserKnownHostsFile /tmp/codex-rpi-ssh/known_hosts
    GlobalKnownHostsFile /dev/null
    HostKeyAlgorithms ssh-ed25519
    StrictHostKeyChecking yes

    BatchMode yes
    ForwardAgent no
    ForwardX11 no
    ClearAllForwardings yes
    ControlMaster no
    ControlPath none
    ConnectTimeout 10
EOF
chmod 600 "$config_tmp"
if [[ -e $CFG/config ]]; then
    cmp -s "$config_tmp" "$CFG/config" ||
        die "$CFG/config já existe com conteúdo diferente; revise-o manualmente."
else
    mv -- "$config_tmp" "$CFG/config"
fi
chmod 600 "$CFG/config" "$CFG/known_hosts"
ssh -G -F "$CFG/config" rpi-sala >/dev/null
printf 'Preparação concluída. Execute ops/launch_codex_rpi.sh --check no terminal do PC.\n'
