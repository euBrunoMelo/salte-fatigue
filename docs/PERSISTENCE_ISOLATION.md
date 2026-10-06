# Persistência isolada da avaliação

`RuntimeApplication` usa `AsyncStructuredLogger` e `AsyncDangerVideoRecorder`.
Os escritores síncronos anteriores permanecem para compatibilidade dos testes
de formato e publicação; o runtime de produção não os chama no loop.
EAR, PERCLOS, recuperação crítica, câmera e codecs não foram alterados.
Esta entrega não instala nem reinicia serviços no Raspberry.

## Admissão e confirmação

O produtor gera IDs e timestamps de origem. `Receipt.accepted` informa apenas
admissão; não é um caminho de arquivo. `receipt_state()` do logger distingue
`rejected`, `enqueued`, `consumed`, `persisted` e `outcome_unknown`. A última
confirmação durável está na saúde do escritor. Confirmações já observadas
continuam válidas após a troca de processo.

O logger escreve JSONL em diretórios `.partial`. Publica segmentos com hashes,
manifesto e `fsync`, preservando pares frame/assessment e amostragem. Eventos e
metadados usam IDs imutáveis. Uma repetição após perda de confirmação verifica
o conteúdo existente e sincroniza o diretório antes de confirmar; não substitui
o arquivo. `run-end` espera os pares admitidos anteriormente e informa perdas
e a situação do vídeo observada na admissão do encerramento. Não confirma uma
execução encerrada sem lacunas se essa informação ainda estiver pendente.

O recorder distingue admissão, submissão ao encoder, número de frames realmente
decodificados na validação, finalização, confirmação durável e cobertura.
`file_finalized=true` não implica `coverage_complete=true`. A confirmação
durável aparece em `last_incident.durably_published` após publicação e `fsync`.
O arquivo final registra `durable_confirmation=worker_ack`, sem tratar a
admissão como confirmação. `logical_event_ids` são referências de correlação
ainda não confirmadas; `event_ids` não é preenchido com referências da fila.

## Memória e saturação

| Recurso | Padrão | Configuração |
|---|---:|---|
| Registros, controles, índices, saúde, scratch e notificações | 32 MiB | `--persistence-record-mib` |
| Todas as imagens próprias em pré-buffer, fila e processamento | 128 MiB | `--persistence-image-mib` |
| Crescimento do espaço virtual do encoder/validador após inicialização | 64 MiB | `--danger-encoder-allowance-mib` |
| Quantidade máxima de pares de telemetria | 16.384 | limite interno |
| Quantidade máxima de eventos / mensagens de console | 512 / 512 | limites internos |
| Quantidade máxima de comandos do recorder | 256 | limite interno |

Os limites de bytes prevalecem sobre quantidades. Slots fixos têm payloads de
8.192 bytes (pares), 4.096 (eventos/controles) e 2.048 (console), além de 32 bytes
de índice. Partições reservadas impedem telemetria e vídeo de consumir espaço
de eventos. Entradas grandes ou profundas são rejeitadas antes de formar cópias
JSON grandes. Scratch de serialização, hashes, índices Python e notificações
tem reserva própria dentro do orçamento de registros. Os sockets de aviso têm
buffers pequenos e transportam somente um byte, sem imagens ou JSON.

`memfd`/`mmap` permitem uma única cópia da câmera para um slot privado. A reserva
precede `np.copyto`; o produtor não guarda o buffer da câmera para esperar disco.
O encoder recebe uma view somente de leitura. Não há fila com feeder ou cópias
de imagem no IPC. Nenhum lock de metadados é mantido durante disco/encoder.
Locks entre processos usam descrições de arquivo distintas e a aquisição no
produtor é não bloqueante; contenção também é uma rejeição explícita.

A 640×480×3, cada frame ocupa 921.600 bytes; cabem 145 slots no pool padrão.
O pré-roll de três segundos a 30 fps retém aproximadamente 90–91 frames,
delimitados por timestamps. Isso deixa cerca de 1,8 segundo de capacidade
adicional. Um bloqueio de 40 segundos causa perdas controladas; aumentar RAM
para escondê-lo não faz parte da solução.

Dados aceitos não são removidos para admitir dados novos. Slots de log ficam
retidos até confirmação durável; slots de imagem são liberados após consumo,
que ainda não confirma persistência. BEGIN/END têm capacidade reservada juntos.
Sem capacidade para começar um incidente, o vídeo fica indisponível e o
evento correspondente tenta o canal independente de logs. Cada frame recusado
continua passando pela avaliação ocular. A saúde contém contadores por
categoria/motivo, primeira perda, resultados desconhecidos e última confirmação.
O escritor publica resumos explícitos de perdas ao retomar, fora das filas
saturadas. Frames expirados normalmente do pré-roll não contam como descarte.

Os 160 MiB são orçamento de dados próprios de persistência, não RSS total.
Python, bibliotecas, câmera/MediaPipe e codecs têm consumo separado. A saúde
expõe RSS e PSS (anônimo, arquivos e memória compartilhada), além da base do
recorder. Não somar RSS dos processos para contar arenas compartilhadas.
O recorder aplica `RLIMIT_AS` à base virtual já inicializada mais a reserva
configurada. O validador usa um thread FFmpeg e mantém mp4v/XVID. A opção
`CAP_PROP_N_THREADS` existe desde o OpenCV 4.8 usado pela imagem
([fonte oficial](https://github.com/opencv/opencv/blob/4.8.0/modules/videoio/include/opencv2/videoio.hpp)).

## Janelas e cobertura

Pré-roll, CRITICAL, pós-roll e cooldown são decisões do produtor usando tempo
de origem. O escritor não recalcula fadiga nem usa atraso de consumo como
tempo do incidente. Cada arquivo tem `timestamps.jsonl`, com ID do incidente,
sequência de admissão e timestamp de cada frame submetido ao encoder.

Metadados guardam janela solicitada, trigger, primeiro/último timestamp real,
admitidos/consumidos/verificados, perdas, motivos, quantidade e maior gap.
Cobertura completa exige contabilização conhecida, começo e fim cobertos,
intervalos internos válidos e ausência de perdas. A tolerância de gap do vídeo
é independente do detector: `--danger-gap-tolerance-sec` (padrão 0,5 s).
Bordas usam um período do fps configurado, ou
`--danger-boundary-tolerance-sec`. Valores e tolerâncias ficam no metadado.
Timestamps repetidos, regressivos ou não finitos são recusados sem alongar
janelas. IDs de eventos por incidente são limitados; excesso marca cobertura
incompleta em vez de acumular referências sem limite.

Morte/erro durante gravação deixa a tentativa interrompida como `.partial`.
Frames retidos podem formar outra parte imutável do mesmo incidente; cobertura
fica incompleta. Uma publicação já finalizada com confirmação perdida é
reconciliada, sem produzir outro vídeo. A exclusão de `.partial` no sync
continua válida. Consumidores devem validar hashes e metadados, e avaliar
`coverage_complete` separadamente da existência do vídeo.

## Saúde, recuperação e encerramento

O HUD e um socket Unix abstrato exibem estado ocular atual e falhas técnicas
separadamente. O socket atende o mesmo UID, sem arquivo de status ou porta de
rede, dentro do namespace local. Nome configurável para múltiplas instâncias:
`--persistence-status-socket @nome`. Exemplo de leitura no mesmo namespace:

```python
import json, socket
with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(1)
    client.connect("\0salte-fatigue-status")
    print(json.loads(client.recv(65536)))
```

O snapshot não espera filas nem disco. Um escritor sem progresso por dez
segundos aparece como `stalled`. Não há substituição de processo vivo bloqueado.
Após morte confirmada, o supervisor tenta no máximo duas substituições, com
esperas de 1 e 5 segundos. Erros de I/O também têm duas tentativas de recuperação;
esgotamento preserva dados admitidos em RAM e mantém falha explícita até o
encerramento. Nenhuma recuperação reinicia a FSM ocular. Console atrasado leva
timestamp original e `historical=true`, e não aciona alerta atual.

O fechamento dos escritores tem prazo global de cinco segundos no aplicativo,
incluindo terminação. Pendências não recebem confirmação durável nem indicação
de história completa. A morte do processo principal termina seus escritores;
não há joins automáticos sem prazo. `--no-danger-record` cria zero processos,
arenas, pré-buffer, encoders, scans ou cópias destinadas ao vídeo.

## Validação local

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/validate_persistence_isolation.py \
  --output docs/evidencias/2026-10-06/persistence_isolation
```

As reproduções usam `RuntimeApplication._publish()` e `FatigueRuntime` reais,
sem câmera/MediaPipe. O mesmo teste e hash reproduzem bloqueio de 40 segundos
em eventos, abertura, escrita e finalização: quatro falhas na base anterior e
aprovação após a mudança. O ensaio integrado bloqueia simultaneamente os dois
escritores por 40 segundos, força filas cheias e lê a saúde durante a falha.
Mede p50/p95/p99/máximo, CPU de entrega/cópia e PSS separado. Resultados são do
PC informado no artefato; não constituem benchmark ou implantação no Raspberry.
Diffs por logger, recorder e integração, logs e campos serializados ficam na
[pasta de evidências](evidencias/2026-10-06/persistence_isolation/).
