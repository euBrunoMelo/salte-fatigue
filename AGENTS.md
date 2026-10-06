# Raspberry da sala

Na sessão preparada com `ops/launch_codex_rpi.sh`, acesse o Raspberry com:

```bash
ssh -F /tmp/codex-rpi-ssh/config rpi-sala 'COMANDO'
```

Não altere a configuração SSH, a VPN nem `authorized_keys`. Não execute `sudo`
ou operações destrutivas no Raspberry sem autorização explícita do usuário.
