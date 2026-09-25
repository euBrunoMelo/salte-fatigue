# Validação do Raspberry Pi para envio

> **Correção de nomenclatura em 2026-09-25:** a CM5IO oficial liga CAM/DISP0
> a `i2c@88000`/barramento 10 e CAM/DISP1 a `i2c@70000`/barramento 0.
> Os relatos históricos abaixo chamam essas portas ao contrário; seus IDs de
> barramento e resultados de captura são as evidências a considerar. A falha
> `-121` ocorreu na CAM/DISP0. Para as versões corrigidas, pose usa `88000` e
> FATIGUE ocular usa `70000`. As recomendações antigas de implantação abaixo
> foram substituídas por esta correção e pelo procedimento atual de Docker
> isolado no armazenamento do sistema.

Atualizado em 2026-09-25. Este documento registra evidência observada e os
critérios que ainda precisam ser cumpridos. O método ocular foi adaptado do
artigo em `docs/references/`; este produto não reproduz os módulos de boca,
frequência cardíaca ou máscara do estudo.

## Estado observado

- O código produtivo do FATIGUE no Pi tinha os mesmos hashes do PC, mas o
  container `salte-tev8` executava uma imagem antiga. Ele foi parado após
  reinícios repetidos por tentar selecionar o índice 1 quando só havia uma
  câmera enumerada.
- Após a troca cruzada dos conjuntos com o Pi desligado, a única IMX500
  detectada passou da CAM1 para a CAM0 (`i2c@70000`). `rpicam-hello --camera 0`
  e `rpicam-still --camera 0` capturaram; a imagem de 4056x3040 está arquivada
  fora do projeto. Naquele boot, a CAM1 apresentou erro `-121` no probe do
  `rp2040-gpio-bridge`, e o índice 1 não captura. A falha acompanha o conjunto
  câmera+cabo retirado da antiga CAM0, não a porta CAM0; as duas portas já
  capturaram com o conjunto bom. A imagem mostrou o piso; é preciso apontar
  a câmera para o rosto antes da calibração e do HIL ocular.
- Em seguida, o usuário trocou somente os módulos, com o Pi desligado, e
  manteve cada cabo em sua porta. A única IMX500 continuou na CAM0
  (`i2c@70000`): `rpicam-hello`, `rpicam-still` (JPEG 4056x3040) e cinco
  frames RGB888 640x480 pelo Picamera2 passaram. `--camera 1` retornou
  `selected camera is not available`; a ponte RP2040 da CAM1 falhou no probe
  com `-121`. Assim, os dois módulos já capturaram pelo cabo da CAM0, e a
  falha ficou no cabo da CAM1 ou em seu encaixe. A porta CAM1 já funcionou
  com o outro cabo. A nova foto também mostra o piso; não permite avaliar o
  rosto ou o método ocular.
- O usuário informou ter substituído o cabo flat da CAM1 com o Pi alimentado;
  houve reinício em seguida. Esse reinício não serve como evidência de falha
  espontânea de software. Após o boot, o novo cabo também não permitiu a
  enumeração da CAM1: a ponte RP2040 voltou a falhar com `-121`, e
  `--camera 1` retornou indisponível. A CAM0 capturou. Em seguida, o usuário
  conferiu e reassentou o cabo da CAM1 com o Pi desligado; no boot posterior,
  a CAM1 continuou ausente. Não houve novo reinício por solicitação do usuário.
- Durante um teste subsequente da CAM0 com Picamera2 em BGR888 640x480, o
  usuário removeu fisicamente câmeras; a captura travou e o kernel registrou
  erro I²C no RP2040 e aviso em `cfe_stop_streaming`. O usuário confirmou que
  a remoção causou o reinício. Esse ensaio é inválido para julgar a estabilidade
  da API ou do conjunto, e deve ser repetido com os cabos fixos. A política
  PCIe também estava temporariamente em `performance` nesse momento.
- Com os cabos imóveis e política PCIe original, Picamera2 capturou cinco
  frames RGB888 640x480 e cinco BGR888 640x480 da CAM0. Foi apenas um teste
  isolado dos formatos de captura; nenhum frame da CAM0 foi entregue ao modelo
  ocular FATIGUE. O detector de pose ainda não foi iniciado com o patch novo.
  O ganho analógico fixado em 1.0 no detector de pose deixou a média do frame
  em ~24; deixando o ganho automático, foi ~109 na mesma cena (ganho medido 8.0).
  A retirada do ganho fixo está no patch preparado, ainda não implantado.
- O kernel registrou repetidos timeouts de escrita no NVMe, inclusive após
  reinício. O SMART informou saúde `PASSED`, zero erros de mídia e 77
  desligamentos inseguros. Esse resultado SMART não elimina os timeouts
  observados pelo kernel. O boot desabilita o buffer de memória host do SSD
  (`nvme.max_host_mem_size_mb=0`); o kernel informa que o mínimo pedido pelo
  dispositivo é 32 MiB. É uma hipótese de diagnóstico, ainda não uma causa
  demonstrada. No boot seguinte, 64 MiB de buffer foram alocados, mas ocorreram
  timeouts NVMe de leitura, escrita e comando administrativo. O ajuste não
  resolveu o defeito; o cmdline original foi restaurado e o Pi já reiniciou com
  o limite original. Houve novos timeouts neste boot e no boot posterior à
  troca somente dos módulos (escritas já nos primeiros 185 s). No boot após
  instalar o cabo novo, voltaram a ocorrer timeouts de escrita aos ~249 s,
  com temperatura de 30,7 °C e `get_throttled=0x0`. Um primeiro teste temporário
  com ASPM em `performance` foi interrompido pela remoção física das câmeras e
  não permite conclusão sobre estabilidade do SSD. Neste boot, sem mexer nas
  câmeras ou reiniciar, a contagem de timeouts NVMe ficou em 19 durante cerca
  de dez minutos com ASPM temporariamente em `performance` (link com ASPM
  desabilitado). Após restaurar `powersave` às 13:53:10, ocorreu novo timeout
  de escrita às 13:54:52 e outro às 13:55:31. Uma tentativa de listar a árvore
  do NVMe travou e coincidiu com timeout de leitura às 14:00:01; foi
  interrompida sem reiniciar o Pi. A diferença sugere testar ASPM como fator,
  mas uma janela curta sem carga controlada não prova a causa nem a
  confiabilidade do SSD; o Pi permanece na política original `powersave`.
  Referência de caso semelhante:
  <https://github.com/raspberrypi/linux/issues/7445>.
- O Compose atual mantém a gravação de incidentes do FATIGUE desligada até
  validar codec e permissões no hardware final. Não há calibração ocular
  instalada.
- O outro detector escreve `eventos.csv` por append e AVI diretamente no nome
  final. O sincronizador com `--ignore-existing` não atualiza o CSV já recebido
  e pode copiar um AVI antes do fechamento. Corrigir publicação e transporte
  antes de aprovar a entrega dos dados.
- O filtro atual já foi instalado no Pi para excluir esse CSV e vídeos
  `*.partial.avi`; o sincronizador antigo foi retirado após cópia e verificação
  dos arquivos. A correção da publicação do detector de pose está preparada em
  `ops/drowsy_persistence.patch`, com segmentos de até 60 s e reserva mínima de
  10 GiB livres, mas ainda não foi aplicada ao NVMe instável.
- O offload genérico está pausado no timer e no dispatcher até haver
  autorização específica para transmitir e verificação do receptor. O último
  status automático anterior à pausa retornou sucesso, sem arquivos enviados;
  isso não comprova integridade no destino.
- A auditoria direta do receptor está pendente por falta de acesso SSH com shell.

## Auditoria de software das câmeras, sem novo reinício

- O `config.txt` efetivo usa `camera_auto_detect=0` e carrega
  `dtoverlay=imx500,cam0` e `dtoverlay=imx500,cam1`. O Device Tree marca os
  dois sensores como `okay`. O host usa kernel `6.12.75+rpt-rpi-2712`,
  libcamera `0.7.0+rpt20260205` e Picamera2 `0.3.34`.
- Em `/sys/bus/i2c/devices`, a ponte `0-0040` e o sensor `0-001a` da CAM0
  estão ligados aos drivers. A ponte `10-0040` e o sensor `10-001a` da CAM1
  não estão. A CAM1 retorna `-121` ao ler informações da ponte antes dos
  containers iniciarem. Uma nova tentativa de vincular apenas `10-0040` ao
  driver, sem reiniciar o Pi, repetiu `-121`; a câmera continuou ausente na
  Picamera2. Isso não identifica sozinho o componente físico defeituoso, mas
  descarta o índice de câmera e a imagem Docker como causa direta da falha
  de enumeração observada.
- A imagem parada `salte-tev8:latest` (julho) ainda contém o pipeline MLP e
  usa `--camera-num 1`; o log registra `IndexError` quando só há uma câmera.
  O `face_landmarker.task` tem SHA-256 idêntico no PC, staging, NVMe e imagem;
  a divergência está no código/pipeline da imagem.
  O código atual do PC vincula FATIGUE ao barramento físico `88000` e recusa a
  CAM0 se a CAM1 faltar. A calibração ocular usa o mesmo vínculo. Conferir o
  manifesto do staging em SD antes de qualquer implantação.
- A imagem parada `drowsydriving-monitor:latest` (junho) corresponde ao
  `main.py` no NVMe, mas escolhe índice `0` e tenta fallback OpenCV caso a
  Picamera2 falhe. O patch local agora o vincula ao barramento físico `70000`
  e proíbe fallback para outra câmera no Pi. O patch aplica e compila; não foi
  implantado no NVMe instável. Os dois arquivos ONNX usados por essa imagem
  (`yolov8n.onnx` e `yolov8n-pose.onnx`) existem e tiveram hashes registrados.
- As imagens paradas usam libcamera `0.5.2` e Picamera2 `0.3.31`, enquanto o
  host usa versões mais novas. O libcamera `0.5.2` escreve IDs como
  `1f00088000.i2c`, e o `0.7.0` como `i2c@88000`; os trechos `88000`/`70000`
  cobrem os dois formatos. A compatibilidade de captura precisa de teste após
  rebuild. A falha I²C da CAM1 ocorre antes da execução dessas imagens.
- O boot mais recente registrou novos timeouts NVMe de leitura e escrita já
  com os detectores parados. O diretório raiz do Docker fica no NVMe; não é
  seguro construir ou iniciar as imagens atuais até resolver esse bloqueio.
  O link está em 5 GT/s x1, sem forçar Gen3 no `config.txt`.
- PipeWire/WirePlumber mantêm nós V4L2 abertos, mas `rpicam-hello` capturou
  pela CAM0 com esses processos ativos; não são o bloqueio observado. O
  reprobe da ponte da CAM1 continuou falhando antes da camada de vídeo.
- O Wi-Fi Manager tenta executar `/usr/local/bin/update_models.sh` ao voltar
  para a rede cliente, mas o arquivo está sem permissão de execução (0644).
  Ele faria `git pull`, build e `compose up` dos diretórios antigos FATIGUE e
  DrowsyDriving. A função de chamada foi neutralizada atomicamente no script
  em disco, sem reiniciar o serviço, e o atualizador segue não executável.
  Não habilitar a rotina: restauraria imagens anteriores e escreveria no NVMe
  com timeouts. Neste boot, o Wi-Fi Manager só saiu do AP após registrar a
  desconexão do último cliente; isso explica a demora observada para voltar
  ao SSH.

## Ordem de execução

1. Com o Pi completamente desligado e sem alimentação, inspecionar
   assentamento/orientação do cabo novo da CAM1 e os conectores. Após ligar,
   executar `rpicam-hello --list-cameras` e capturar das duas câmeras,
   registrando o caminho físico de cada uma. Os dois módulos já capturaram
   pelo cabo da CAM0. Não usar o índice como identificador permanente quando
   apenas uma câmera aparece. Apontar a câmera para o rosto antes do HIL ocular.
2. Diagnosticar alimentação, conexão PCIe, adaptador e SSD. Repetir a leitura
   do kernel e SMART. Não construir imagem no NVMe nem iniciar o teste de carga
   enquanto aparecerem novos timeouts de escrita.
3. Construir a imagem FATIGUE deste diretório, conferir digest e conteúdo,
   testar CAM1 com gravação desligada e fazer a calibração aberta/fechada na
   geometria definitiva.
4. Corrigir a publicação do detector de pose: nomes temporários excluídos pelo
   filtro durante a escrita, publicação final somente após fechamento e
   validação, e eventos imutáveis para transporte. Manter a galeria capaz de
   consumir seus dados locais.
5. Com operador diante das câmeras, testar os dois containers simultaneamente,
   os estados de segurança de `TESTE REGRECAO.txt`, FPS e gravação. Depois
   executar pelo menos 24 h de operação e períodos de replay das capturas.
6. Quando houver acesso ao receptor, comparar caminhos, contagens e SHA-256
   dos objetos finais; exercitar queda e retorno da rede. O status local do
   rsync não substitui essa conferência.

## Coleta e aprovação

Guardar a telemetria no PC, sem acrescentar escrita ao NVMe avaliado:

```bash
python3 ops/collect_pi_health.py \
  --known-hosts /tmp/salte_pi_known_hosts \
  --output validation/pi-health.jsonl \
  --duration-seconds 86400
```

Aprovação exige duas câmeras capturando em paralelo, imagem FATIGUE atual,
calibração válida, vídeo e logs legíveis, sincronização íntegra no receptor,
nenhum novo timeout NVMe, OOM, reinício inesperado ou indicação de redução de
desempenho por temperatura. Medir crescimento diário de dados para definir
reserva de disco antes do envio.
