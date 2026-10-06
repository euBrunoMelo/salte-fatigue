#!/usr/bin/env bash
# Prepare LAN access using the Raspberry host key already trusted over VPN.
# Run once in a normal Ubuntu terminal on the room Wi-Fi, outside ai-jail.
set -euo pipefail

VPN_IP=100.120.192.2
LAN_IP=${RPI_LAN_IP:-192.168.15.62}
RPI_USER=raspberrypi5
KEY="$HOME/.ssh/id_ed25519_codex_rpi_sala"
VPN_CFG="$HOME/.config/ai-jail/rpi-sala"
LAN_CFG="$HOME/.config/ai-jail/rpi-sala-lan"

die() {
    printf 'Erro: %s\n' "$*" >&2
    exit 1
}

for tool in bwrap ip ssh ssh-keygen ssh-keyscan; do
    command -v "$tool" >/dev/null || die "Comando ausente: $tool"
done
[[ $(stat -c %u "$(command -v bwrap)") == 0 ]] ||
    die 'Execute no terminal normal do PC, fora de outra jail/container.'
[[ $LAN_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
    die 'RPI_LAN_IP deve ser um endereço IPv4 numérico.'
[[ -f $KEY && -f $KEY.pub && -f $VPN_CFG/known_hosts && -f $VPN_CFG/identity.pub ]] ||
    die 'A preparação VPN existente está incompleta; confira a chave e o known_hosts dedicados.'
cmp -s "$KEY.pub" "$VPN_CFG/identity.pub" ||
    die 'A chave pública existente não corresponde à identidade preparada para a VPN.'

route=$(ip -4 route get "$LAN_IP") || die "Sem rota IPv4 para $LAN_IP."
route_iface=$(awk '{ for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }' <<< "$route")
[[ -n $route_iface && $route != *' via '* &&
   ( -e /sys/class/net/$route_iface/phy80211 || -d /sys/class/net/$route_iface/wireless ) ]] ||
    die "A rota para $LAN_IP não é direta pelo Wi-Fi; não usarei a VPN."

trusted_key=$(
    awk -v ip="$VPN_IP" \
        '$1 == ip && $2 == "ssh-ed25519" { print $2 " " $3; count++ }
         END { if (count != 1) exit 1 }' "$VPN_CFG/known_hosts"
) || die 'Não encontrei exatamente uma chave ed25519 confiável para o IP da VPN.'
lan_key=$(
    ssh-keyscan -T 5 -t ed25519 "$LAN_IP" 2>/dev/null |
        awk -v ip="$LAN_IP" '$1 == ip && $2 == "ssh-ed25519" { print $2 " " $3; exit }'
) || die "Não foi possível consultar a chave SSH de $LAN_IP pela rede local."
[[ -n $lan_key && $lan_key == "$trusted_key" ]] ||
    die 'A chave SSH apresentada na LAN difere da identidade já confiável do Raspberry.'

install -d -m 700 "$LAN_CFG"
known_hosts_tmp=$(mktemp "$LAN_CFG/.known_hosts.XXXXXX")
config_tmp=$(mktemp "$LAN_CFG/.config.XXXXXX")
trap 'rm -f -- "$known_hosts_tmp" "$config_tmp"' EXIT
printf '%s %s\n' "$LAN_IP" "$trusted_key" > "$known_hosts_tmp"
chmod 600 "$known_hosts_tmp"

# Authenticate with the existing private key before publishing the LAN config.
# The private key and its passphrase remain outside the jail.
ssh -F /dev/null \
    -o IdentityAgent=none \
    -o IdentitiesOnly=yes \
    -o PreferredAuthentications=publickey \
    -o PasswordAuthentication=no \
    -o KbdInteractiveAuthentication=no \
    -o HostKeyAlgorithms=ssh-ed25519 \
    -o GlobalKnownHostsFile=/dev/null \
    -o "UserKnownHostsFile=$known_hosts_tmp" \
    -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 \
    -i "$KEY" \
    "$RPI_USER@$LAN_IP" 'hostname; id -un' ||
    die 'A chave exclusiva não autenticou no Raspberry pela LAN.'

cat > "$config_tmp" <<EOF
Host rpi-sala
    HostName $LAN_IP
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
install -m 644 "$KEY.pub" "$LAN_CFG/identity.pub"
mv -f -- "$known_hosts_tmp" "$LAN_CFG/known_hosts"
mv -f -- "$config_tmp" "$LAN_CFG/config"
chmod 600 "$LAN_CFG/known_hosts" "$LAN_CFG/config"
ssh -G -T -F "$LAN_CFG/config" rpi-sala >/dev/null
printf 'LAN preparada para %s@%s. Inicie com: bash ops/launch_codex_rpi.sh --lan --check\n' \
    "$RPI_USER" "$LAN_IP"
