# Offload versionado de Monitoramento

Pacote `1.1.3` para sincronizar arquivos finais de logs de todos os produtos sob
`/mnt/nvme/Monitoramento/`. A origem única termina em `/`, portanto um único
`rsync` preserva no destino a árvore relativa completa de cada produto:

```text
/mnt/nvme/Monitoramento/
  ProdutoA/logs/...       rsync/SSH       salte@office.salte.me:
  ProdutoB/logs/...  ------------------> /home/salte/Britago/
```

Não há fallback de endpoint. Indisponibilidade de DNS, SSH ou destino é uma
falha observável: o script registra o resultado via `logger`, atualiza o status
e retorna o exit code não-zero do `rsync`.

## Ativos da versão 1.1.3

| Arquivo | Instalação | Papel |
|---|---|---|
| `monitoramento-sync.sh` | `/usr/local/sbin/monitoramento-sync.sh` | Um lock, uma seleção e um `rsync` |
| `rsync_filter.conf` | `/etc/monitoramento-sync/rsync_filter.conf` | Contrato externo de inclusão/exclusão |
| `monitoramento-sync.service` | `/etc/systemd/system/` | Execução oneshot com mount e timeout |
| `monitoramento-sync.timer` | `/etc/systemd/system/` | Agenda alinhada a cada cinco minutos |
| `99-monitoramento-sync` | `/etc/NetworkManager/dispatcher.d/` | Solicita o mesmo service para qualquer interface `up` |
| `wifi-manager-logrotate` | `/etc/logrotate.d/wifi-manager` | Limita e comprime o log operacional do Wi-Fi |
| `VERSION` | `/usr/share/monitoramento-sync/VERSION` | Versão instalada |
| `install-monitoramento-sync.sh` | não instalado | Instalador dos ativos acima |

## Contrato de seleção

As exclusões de `*.partial/`, `*.partial`, `.rsync-partial/` e `quarantine/`
aparecem antes das inclusões no filtro. Apenas arquivos em árvores
`*/logs/**` são elegíveis. O script usa `--partial-dir=.rsync-partial`, mas não
remove originais, não altera arquivos no lugar, não substitui nomes remotos
existentes e não apaga o arquivo remoto. `--delay-updates` adia os nomes finais
dos arquivos recebidos até o fim da transferência.

Essa garantia é por arquivo. O rsync não publica atomicamente um diretório
inteiro; consumidores devem validar `manifest.json` ou `metadata.json` e os
arquivos referenciados antes de consumir um segmento ou incidente completo.
O CSV mutável `DrowsyDriving/logs/eventos.csv` e vídeos `*.partial.avi` ficam
fora do transporte. Novos eventos do detector de pose devem ser documentos
imutáveis em `DrowsyDriving/logs/events/`; os AVI são publicados no nome final
somente depois de fechados e validados.

`eligible_files` é contado diretamente na árvore local com `find`, excluindo o
CSV mutável e os vídeos parciais acima. Ele mede os arquivos finais elegíveis,
não arquivos pendentes no receptor. Deliberadamente
não há uma segunda execução `rsync --dry-run` para estimar pendências.

## Concorrência, SSH e estado

`flock` impede sobreposição entre timer e dispatcher. A conexão usa uma chave
dedicada, `BatchMode=yes`, `StrictHostKeyChecking=accept-new` e
`ConnectTimeout`; o transporte também tem timeout do `rsync`. O script nunca lê
senha, nunca habilita tracing e não registra conteúdo da chave.

A chave `monitoramento_sync` deve ser restrita no receptor com
`command="/usr/bin/rrsync -wo /home/salte/Britago",restrict`. Por isso o destino
SSH do script é `salte@office.salte.me:./`: o caminho relativo representa a raiz
`/home/salte/Britago` imposta pelo receptor, sem conceder shell remoto.

Cada disparo executa no maximo tres tentativas, com 30 segundos entre elas. O
timer continua sendo a rede de seguranca apos o esgotamento do retry interno.

O status padrão é `/var/lib/monitoramento-sync/status.json`. A escrita usa um
temporário no mesmo diretório seguido de `mv`, e mantém o último sucesso quando
uma tentativa falha:

```json
{
  "version": "1.1.3",
  "last_attempt": "2026-09-11T12:00:00Z",
  "last_success": "2026-09-11T11:55:02Z",
  "duration": 2,
  "eligible_files": 42,
  "files_transferred": 3,
  "bytes_transferred": 8192,
  "attempts": 1,
  "rsync_exit_code": 0,
  "destination": "salte@office.salte.me:./"
}
```

`duration` é expresso em segundos. `files_transferred` e `bytes_transferred`
consideram arquivos regulares efetivamente reportados pela execução real do
`rsync`; `bytes_transferred` é a soma dos tamanhos lógicos desses arquivos.

## Overrides para teste

Todos os defaults operacionais permanecem fechados no script. Testes locais
podem sobrescrever somente por ambiente:

| Variável | Default |
|---|---|
| `MONITORAMENTO_SYNC_SOURCE` | `/mnt/nvme/Monitoramento/` |
| `MONITORAMENTO_SYNC_DESTINATION` | `salte@office.salte.me:./` |
| `MONITORAMENTO_SYNC_FILTER_FILE` | `/etc/monitoramento-sync/rsync_filter.conf` |
| `MONITORAMENTO_SYNC_STATUS_FILE` | `/var/lib/monitoramento-sync/status.json` |
| `MONITORAMENTO_SYNC_LOCK_FILE` | `/var/lib/monitoramento-sync/sync.lock` |
| `MONITORAMENTO_SYNC_SSH_KEY` | `/home/raspberrypi5/.ssh/monitoramento_sync` |
| `MONITORAMENTO_SYNC_SSH_PORT` | `2222` |
| `MONITORAMENTO_SYNC_CONNECT_TIMEOUT` | `10` |
| `MONITORAMENTO_SYNC_TIMEOUT` | `60` |
| `MONITORAMENTO_SYNC_MAX_ATTEMPTS` | `3` |
| `MONITORAMENTO_SYNC_RETRY_DELAY` | `30` |
| `MONITORAMENTO_SYNC_RSYNC_BIN` | `rsync` |
| `MONITORAMENTO_SYNC_SSH_BIN` | `ssh` |
| `MONITORAMENTO_SYNC_LOGGER_BIN` | `logger` |

Não coloque senha, token ou conteúdo de chave nessas variáveis. A autenticação
de produção é exclusivamente pela chave dedicada indicada pelo caminho.

## Instalação e gates

O instalador só copia os ativos versionados e ajusta owner/mode. Ele não chama
`systemctl`, não habilita nem desabilita units e não altera chave SSH:

```bash
sudo sync/install-monitoramento-sync.sh
```

Antes da ativação, valide obrigatoriamente:

1. O mount `/mnt/nvme/Monitoramento` existe e contém as árvores esperadas.
2. A chave dedicada pertence a `raspberrypi5` e está restrita no receptor.
3. O receptor permite preservar a raiz `/home/salte/Britago/` sem achatar produtos.
4. `.venv/bin/python -m unittest discover -s tests -p 'test_sync_contract.py' -v` passa.
5. `bash -n` passa individualmente para cada `sync/*.sh` e `sync/99-*`.
6. Uma execução manual retorna `0` e produz status coerente sem expor segredo.

Depois dos gates, a ativação continua sendo decisão explícita do operador:

```bash
sudo systemctl daemon-reload
sudo systemctl start monitoramento-sync.service
sudo systemctl enable --now monitoramento-sync.timer
journalctl -t monitoramento-sync -n 50
```

O dispatcher não executa o script diretamente: para toda interface cujo evento
seja `up`, solicita de forma não bloqueante o mesmo
`monitoramento-sync.service`. O `flock` continua sendo a proteção final contra
gatilhos simultâneos.
