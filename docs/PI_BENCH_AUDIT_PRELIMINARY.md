# Auditoria preliminar dos ensaios no CM5IO

Atualizada em 2026-10-02. Este inventário usa **os relatos preservados neste
repositório**, não uma inspeção do estado atual do Pi. O pacote remoto gerado por
`ops/collect_pi_bench_evidence.py` deverá ser confrontado com cada linha antes
de concluir a auditoria. Arquivos de imagem citados nos relatos estão fora do
repositório. Não há aqui uma medição elétrica do conector durante o probe.

**Consulta atual fornecida pelo operador:** saída colada do terminal do Pi em
2026-10-02 às 13:11:59 -03: `hostname=raspberrypi`, kernel
`6.12.75+rpt-rpi-2712`, boot ID
`0a2b9133-9d73-474c-b524-333d30052010`. `journalctl --list-boots` mostrou
somente esse boot, de 2026-09-25 16:46:56 -03 até 2026-10-02 13:11:54 -03.
Esta é evidência comunicada pelo operador, ainda sem exportação bruta ou hash.
O journal disponível permite analisar eventos deste boot, mas não recuperar
por esse meio um primeiro probe de boots anteriores.

Uma tentativa posterior de consultar `journalctl`, sysfs e `rpicam-hello`
recebeu marcadores de citação (`[cite: 2]`) anexados aos comandos copiados.
Todos retornaram erros de sintaxe ou de opção. Essa tentativa não gerou
evidência sobre o hardware e deve ser repetida com comandos limpos.

**Log RP2040 fornecido pelo operador no mesmo boot:** `journalctl -k -b
--no-pager -g rp2040` retornou quatro linhas, todas em 2026-09-25 16:46:56:
`0-0040` identificou dispositivo `27133e63c52864eb`, firmware v15, e
registrou `Could not acquire fast_xfer-gpios`; `10-0040` registrou `Could not
get device info` e falha de probe `-121`. A saída enviada não contém outra
linha RP2040 posterior neste boot. Esse log documenta a falha no primeiro
probe, mas a permanência sem driver dias depois não constitui uma nova
tentativa; a versão da ponte `10-0040` continua desconhecida.

**Vínculos atuais fornecidos pelo operador:** `ls -l` mostrou
`0-001a → imx500` e `0-0040 → rp2040-gpio-bridge`; os links `driver` de
`10-001a` e `10-0040` não existem. Os carimbos de data dos symlinks não foram
usados como horário de probe ou rebind. Ainda falta a enumeração por libcamera.

**Enumeração atual fornecida pelo operador:** `rpicam-hello --list-cameras`
mostrou uma única IMX500, índice 0, com caminho físico
`/base/axi/pcie@1000120000/rp1/i2c@70000/imx500@1a` e modos
2028×1520/30,02 fps e 4056×3040/10,00 fps. O índice 0 neste resultado é
CAM/DISP1; não há câmera enumerada em CAM/DISP0. Enumeração não comprova
captura atual nem estabilidade sob carga.

**Configuração textual fornecida pelo operador:** o recorte de
`/boot/firmware/config.txt` contém `camera_auto_detect=1` na linha 18 e
`camera_auto_detect=0` na linha 53; os overlays não comentados são
`dtoverlay=imx500,cam0` e `dtoverlay=imx500,cam1` nas linhas 56–57. Os
overlays `imx500-pi5` nas linhas anteriores estão comentados. O arquivo
completo confirmou `[cm5]` na linha 49 e `[all]` na 52; a seção `[all]` aplica
as linhas seguintes a todos os modelos. A presença dos quatro dispositivos
I²C em sysfs mostra que as declarações de ambos os conjuntos foram
instanciadas neste boot. Isso afasta a hipótese simples de overlay ausente,
sem provar que o sequenciamento do kernel/Device Tree está correto.
Referência: [documentação oficial de `config.txt`](https://www.raspberrypi.com/documentation/computers/config_txt.html).

**Boot e PCIe atuais fornecidos pelo operador:** `/proc/cmdline` contém
`nvme.max_host_mem_size_mb=0`; a política em
`/sys/module/pcie_aspm/parameters/policy` aparece como
`default performance [powersave] powersupersave`, portanto `powersave` está
selecionada no instante da consulta; `vcgencmd get_throttled` retornou `0x0`.
Essas leituras corroboram a restauração dos ajustes relatada em 25/09, mas
não demonstram estabilidade do SSD nem tensão de alimentação durante erros.

**NVMe ainda falhando em 2026-10-02:** o operador forneceu as últimas 80
linhas NVMe de `journalctl -k -b -o short-iso-precise`. Entre 11:07:59 e
13:27:59 -03 aparecem timeouts de comando administrativo (`opcode 0x6`, QID
0) seguidos de reset do controlador aproximadamente a cada dez minutos.
Também aparecem timeouts de escrita de 4 ou 8 KiB às 11:19, 11:49, 12:19,
12:49 e 13:19, seguidos por `Abort status: 0x0`. Após cada reset o kernel
registra `min host memory (32 MiB) above limit (0 MiB)` e recria as filas.
Esse padrão é observado no recorte, não uma contagem total do boot nem prova
de qual serviço ou componente dispara os comandos. O gate de armazenamento
de `TESTE REGRECAO.txt` continua não atendido; não iniciar teste de carga ou
gravação no NVMe antes de isolar esses timeouts.

**Timers consultados:** `systemctl list-timers --all --no-pager` mostrou nove
timers (limpeza de temporários, apt, zram writeback, backup dpkg, logrotate,
man-db, e2scrub e fstrim). Nenhum deles aparece agendado a cada dez minutos.
Isso não exclui cron, um processo residente ou operação interna do kernel;
não atribui os resets a um serviço específico.

**Contexto de um reset:** no intervalo 2026-10-02 13:17:45–13:18:15 -03, o
journal fornecido pelo operador registra o timeout de comando administrativo
do NVMe às 13:17:59.886 e, 27 ms depois, `udisksd[838]` informa falha no
housekeeping ao atualizar Health Information: `NVMe Identify Controller
command error: Interrupted system call`. A rotina de UDisks2 estava envolvida
na consulta administrativa desse episódio. Isso não demonstra que UDisks2
causou a falha de resposta do controlador nem explica os timeouts de escrita.
Verificar recorrência da mesma associação nos outros intervalos.

**Recorrência verificada:** as 40 linhas mais recentes de
`journalctl -b _COMM=udisksd` fornecidas pelo operador mostram a mesma falha
de housekeeping/`NVMe Identify Controller` aproximadamente a cada dez
minutos, de 06:57:59 a 13:27:59 -03 em 2026-10-02. Os horários coincidem com
os resets administrativos do recorte de kernel; isso identifica UDisks2 como
origem provável das consultas periódicas. O projeto UDisks descreve um ciclo
de housekeeping de dez minutos em seu [rastreador oficial](https://github.com/storaged-project/udisks/issues/892).
Ainda não há evidência de que desativar essas consultas eliminaria timeouts de
escrita, nem de que UDisks2 seja a causa da falta de resposta do controlador.

**Contexto das escritas:** no journal de 2026-10-02 13:18:55–13:20:10 -03
fornecido pelo operador aparecem somente os timeouts `req_op:WRITE` de 8 KiB
às 13:19:17.710 e 4 KiB às 13:19:56.622, cada um seguido de `Abort status:
0x0`. O journal nesse intervalo não identifica o processo solicitante.
Não atribuir essas escritas ao housekeeping de UDisks2 apenas pela
proximidade temporal.

**Inventário inicial de arquivos no diretório do usuário:** uma busca por
nomes até profundidade 4 encontrou os relatórios de 25/09, o protocolo de
regressão e testes de código no staging do FATIGUE, além de
`~/Documentos/DeteccaoFadigaORIGINAL/teste.jpg`. A busca foi limitada ao
filesystem do diretório do usuário e a nomes específicos; não comprova que
não existam outros registros. Entradas de `.ssh` apareceram por coincidência
de nome e foram excluídas das próximas coletas; nenhuma chave foi aberta.

**Detector original em `~/Documentos/DeteccaoFadigaORIGINAL`:** a listagem até
profundidade 2 mostrou README, scripts de inferência e avaliação offline,
modelos ONNX, configuração de inferência e `teste.jpg`. Não apareceu nessa
profundidade um relatório de bancada separado; o README e a imagem ainda não
foram inspecionados. O conteúdo de `.git` e `.venv` não é evidência de ensaio.

**README do detector original fornecido pelo operador:** descreve pipeline
MLP V3 com 21 features, calibração de 120 s, janela de 15 s e um comando para
uso com Picamera2. Relata métricas de validação cruzada em quatro folds do
experimento TEV7 (balanced accuracy 67,3%, F1 Danger 74,1%, AUC 71,6%), mas
não inclui saída de execução, boot ID, câmera física nem resultado de teste
de bancada neste Pi. `offline_eval.py` é uma capacidade de avaliação offline,
não prova de que ela foi executada aqui. Esse pipeline é anterior ao runtime
EAR/PERCLOS determinístico deste projeto e não deve ser contado como HIL
atual. A imagem `teste.jpg` ainda aguarda identificação.

**Metadados de `teste.jpg` fornecidos pelo operador:** JPEG 4056×3040,
1.943.427 bytes, mtime 2026-02-27 15:38:23 -03 e SHA-256
`332ade600ada7d1ea8c8ed91e8cdddbced3a2b86a08aa5b845b180b78513c921`.
A data disponível é anterior aos ensaios relatados de setembro. Sem a imagem
e sua proveniência, não atribuí-la a uma porta/cabo da montagem atual nem
contá-la como validação visual do rosto.

**Logs brutos do FATIGUE localizados no Pi:** em
`~/salte-fatigue-staging-20260925/logs/runs/2026/09/25/52b4734cad3c4720bd22b6346fdb8fc0/`
há `run-start.json`, segmentos `000001` a `000005` com `frames.jsonl`,
`assessments.jsonl` e `manifest.json`, e `000006.partial` com JSONL mas sem
manifesto na listagem recebida. A primeira busca, limitada à profundidade 5,
não alcançava esses arquivos; a busca de profundidade 9 os encontrou. A
existência dos cinco manifestos indica publicação de cinco segmentos, mas
contagens, hashes internos e motivo da permanência do parcial ainda precisam
ser conferidos. Não inferir encerramento limpo apenas pelo diretório presente.

**Conteúdo dos manifestos fornecido pelo operador:** `run-start.json` inicia
em 2026-09-25 19:19:24.953 UTC, pede 30 fps, registra `camera_num:1`,
`calibration_loaded:false`, fallback EAR nulo e PERCLOS de 60 s. Cinco
manifestos com `status:completed` fecharam às 19:24:24, 19:29:25,
19:34:25, 19:39:25 e 19:44:25 UTC. Cada um declara 1.801 frames e 1.801
avaliações; no total são 9.005 pares **registrados**, não necessariamente o
total de frames processados. As contagens de linhas ainda precisam ser
confirmadas. Os intervalos de índices avançam em passos de cinco e o Compose
local contém `--log-frame-every 5 --log-assessment-every 5`; logo, a amostragem
explica os 1.801 pares por segmento. O código resolve a câmera pela ID
`70000` antes de abrir Picamera2; `camera_num:1` em `run-start.json` é o valor
do argumento registrado, não prova de que o índice 1 foi aberto.

**Contagens de linhas fornecidas pelo operador:** cada um dos segmentos
`000001`–`000005` contém 1.801 linhas em `frames.jsonl` e 1.801 em
`assessments.jsonl`, confirmando as contagens declaradas nos manifestos.
`000006.partial` contém 247 linhas de frames e 237 de avaliações: há diferença
de dez linhas e nenhum manifesto de conclusão. O segmento parcial não deve
ser tratado como conjunto íntegro para consumo ou sincronização. A causa da
diferença depende do estado do processo e das datas dos arquivos.

**Integridade dos documentos no staging:** o operador calculou SHA-256 dos
quatro textos de 25/09 em `~/salte-fatigue-staging-20260925`; todos coincidem
com os arquivos locais: diagnóstico `4ab5a833…`, implantação `fa35bd4f…`,
validação `bd013000…` e protocolo `1733373e…`. O staging não contém versões
diferentes desses relatos. Essa comparação de hash não valida os artefatos
brutos ou as medições citadas pelos textos.

## Coleta no computador que já acessa o Pi

Executar na raiz deste projeto, **fora da sessão SSH**. A pasta de saída deve
estar vazia e fora do repositório:

```bash
python3 ops/collect_pi_bench_evidence.py \
  --host raspberrypi5@100.120.192.2 \
  --output-dir ~/pi-bench-20261002
cd ~/pi-bench-20261002
sha256sum -c manifest.sha256
```

O coletor não cria arquivos no Pi nem executa captura, rebind ou reinício.
Ele não percorre o NVMe instável. `collection-index.json` registra consultas
malsucedidas, truncamentos e mudanças de boot ID; um erro parcial não deve ser
interpretado como ausência do evento procurado. Revisar o conteúdo antes de
compartilhar, pois logs e configuração podem conter dados de rede ou do
equipamento. Enviar o diretório compactado ou os arquivos selecionados para
confronto com a matriz abaixo.

## Identificação física

Na CM5IO oficial, **CAM/DISP0 = `i2c@88000` / adaptador 10** e **CAM/DISP1 =
`i2c@70000` / adaptador 0**. Os documentos de 25/09 originalmente chamaram
essas portas CAM1 e CAM0, respectivamente; suas erratas corrigem os nomes sem
alterar os resultados medidos. Pose está destinada à CAM/DISP0 e FATIGUE
ocular à CAM/DISP1. Fonte: `CM5IO_IMPLANTACAO_2026-09-25.md`.

## Matriz do que foi registrado

| ID | Ensaio ou ajuste registrado | Resultado documentado | Evidência e limite |
| --- | --- | --- | --- |
| C01 | Troca cruzada dos conjuntos câmera+cabo com o Pi desligado | A única IMX500 passou para `i2c@70000`; `rpicam-hello` e `rpicam-still` capturaram. O conjunto em `i2c@88000` falhou na ponte com `-121`. | Relato em `VALIDACAO_ENVIO.md`; imagem fora do projeto. Não há aqui log bruto desse boot nem ID dos módulos. |
| C02 | Troca somente dos módulos, mantendo cada cabo em sua porta e desligando o Pi | Os dois módulos capturaram em momentos distintos pelo cabo de `i2c@70000`; `i2c@88000` continuou sem enumerar. | Relato em `VALIDACAO_ENVIO.md`; restringe a hipótese de módulo defeituoso, mas não isola cabo, conector ou porta sob a montagem atual. |
| C03 | Substituição do cabo em `i2c@88000` com alimentação ligada e posterior reassentamento com o Pi desligado | Após ambos os eventos, a ponte ainda falhou com `-121`; houve reinício após a troca energizada. | Relato do operador em `VALIDACAO_ENVIO.md`. O reinício e a remoção energizada impedem usar essa etapa como teste controlado de estabilidade ou causa. |
| C04 | Captura da câmera em `i2c@70000` | JPEG 4056×3040, cinco frames RGB888 e cinco BGR888 640×480 passaram com os cabos imóveis. | `VALIDACAO_ENVIO.md`; as fotos mostram o piso, não validam rosto, calibração nem modelo ocular. Uma captura anterior com remoção física durante o ensaio é inválida para estabilidade. |
| C05 | Nova tentativa de vínculo da ponte `10-0040` com a outra câmera capturando e `cam0_reg` habilitado | Uma escrita em `bind` retornou erro; o kernel repetiu `Could not get device info` e `-121`. | `CAM1_DIAGNOSTICO_2026-09-25.md` contém horário, estado e trecho do log. Não representa desligamento elétrico e inicialização limpa da ponte. |
| C06 | Conferência de overlays, Device Tree e vínculos I²C | Ambos os overlays IMX500 estavam declarados; sensor e ponte de `i2c@70000` vinculados, os de `i2c@88000` sem driver. | `VALIDACAO_ENVIO.md` e `CM5IO_IMPLANTACAO_2026-09-25.md`; fotografia de um boot, não reprodução contínua do erro. |
| N01 | Aumento temporário do buffer de memória host do NVMe para 64 MiB | Houve timeouts de leitura, escrita e comando; parâmetro original restaurado. | `VALIDACAO_ENVIO.md`; ajuste não resolveu os timeouts observados. |
| N02 | ASPM temporariamente em `performance`, depois retorno a `powersave` | Cerca de dez minutos sem novo timeout durante `performance`; novos timeouts após retorno. | `VALIDACAO_ENVIO.md`; janela curta e carga não controlada, portanto causalidade não demonstrada. Uma tentativa anterior foi interrompida pela manipulação das câmeras. |
| N03 | Consulta pontual de temperatura, throttling e SMART | 30,7 °C, `get_throttled=0x0`; SMART `PASSED`, sem erro de mídia, mas o kernel continuou registrando timeouts. | `VALIDACAO_ENVIO.md`; leituras pontuais não comprovam estabilidade da alimentação ou do SSD. |
| R01 | Implantação isolada do FATIGUE no armazenamento do sistema | O container identificou `i2c@70000` e processou 2.406 frames em cerca de 80 s; sem calibração, estado `unknown`. | `CM5IO_IMPLANTACAO_2026-09-25.md`; é teste de funcionamento inicial de uma câmera, não HIL completo nem ensaio simultâneo das duas. |
| R02 | Suíte local de regressão | 127 testes passaram antes da implantação. | `CM5IO_IMPLANTACAO_2026-09-25.md`; valida software local, não hardware de bancada. |

O filtro de sincronização foi instalado, o sincronizador antigo retirado após
cópia e verificação, e o offload genérico foi pausado. O patch do detector de
pose e a correção de publicação de seus dados ficaram preparados, sem
implantação no NVMe instável (`VALIDACAO_ENVIO.md`).

## Ensaios ainda necessários para separar hipóteses

1. Registrar um boot após desligamento e desenergização completos, sem mover
   cabos, com eventos I²C, regulador, GPIO e runtime PM desde o primeiro probe.
   Associar log e trace ao boot ID. Isso separa falha inicial de estado deixado
   por uma tentativa anterior.
2. Se `i2c@88000` continuar falhando, testar o **mesmo módulo e cabo bons**
   sozinho em CAM/DISP1 → CAM/DISP0 → CAM/DISP1, desligando e desenergizando
   entre montagens e usando apenas o overlay da porta em teste. Registrar
   enumeração e captura em cada boot; o padrão funciona → falha → funciona
   concentra a investigação no caminho da porta.
3. Se o padrão persistir, comparar uma instalação oficial independente com
   conjunto e porta constantes. Preparar antes o procedimento de seleção de
   boot e restauração, já que a configuração atual prioriza eMMC.
4. Medir no conector, durante o primeiro probe, alimentação de 3,3 V, linha de
   habilitação e SDA/SCL, junto do trace. A pinagem oficial identifica pino 22
   como 3V3, 20 como SCL e 21 como SDA. Não inferir tensão durante o probe de
   estados de GPIO ou regulador consultados depois da falha.
5. Tratar NVMe separadamente: correlacionar timeouts com alimentação de 5 V,
   estado PCIe, ASPM e carga controlada. Suspender qualquer carga se surgirem
   novos erros de escrita. Depois de estabilizar o armazenamento, executar o
   HIL de duas câmeras, calibração na geometria final e 24 h acompanhadas
   descritos em `TESTE REGRECAO.txt`.

Referências externas: [conector e pinagem da câmera](https://www.raspberrypi.com/documentation/accessories/camera.html),
[arquitetura da AI Camera](https://www.raspberrypi.com/documentation/accessories/ai-camera.html),
[rastreio de eventos no boot](https://docs.kernel.org/trace/boottime-trace.html).

## Pendências documentais

- Associar cada teste anterior a data, boot ID, identidade física dos módulos e
  cabos e artefato bruto. O relato de troca física por si só não fornece essas
  identidades.
- Verificar estado atual da ponte, versão de firmware residente quando legível,
  logs de boots preservados e se o problema reproduz no primeiro probe. O
  pacote de 25/09 não demonstra recuperação ou falha no boot atual.
- Confirmar localização e integridade das fotos, logs de captura, resultados
  SMART e registros do container que os documentos citam mas não incorporam.
- Registrar separadamente testes executados, propostas e ensaios invalidados
  pela manipulação energizada. Não declarar causa física, estado de firmware ou
  falha de kernel/Device Tree antes dos testes discriminantes.
