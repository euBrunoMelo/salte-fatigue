# Implantação no Docker do NVMe — 2026-10-02

Estado observado no CM5 `raspberrypi`, boot ID
`0a2b9133-9d73-474c-b524-333d30052010`, kernel
`6.12.75+rpt-rpi-2712`. O Docker padrão informa
`DockerRootDir=/mnt/nvme/docker`; `/mnt/nvme` está montado em ext4 com
leitura e escrita.

## Código e imagens

O código do FATIGUE em `/mnt/nvme/Monitoramento/FATIGUE` foi alinhado aos
arquivos deste projeto no PC. Os hashes SHA-256 de `Dockerfile`,
`.dockerignore`, `docker-compose.yml`, `run_host.py`, `calibrate_eyes.py`,
`feature_extractor_rt.py`, `camera_routing.py` e `wait_for_camera.py` coincidem
entre PC e Pi após a implantação. Os cinco módulos Python conferidos dentro da
imagem também coincidem. A imagem foi construída no Docker padrão do Pi:

- `salte-fatigue:latest`: `sha256:7b580cc6bd10019362bb30eaf69917e875e91ae84bed2f56bb55a9e2da8dc160`
- `drowsydriving-monitor:latest`: `sha256:627b5612ac9d6f34acdb17e212f7ebec0d0e152dc4b4c93444a309b56a15b5f6`

O projeto de pose usado é `/mnt/nvme/Monitoramento/DrowsyDriving`. Não foi
encontrada uma cópia correspondente em `/home/bruno/Documents` no PC; portanto,
não há comparação PC↔Pi desse projeto. `main.py` no NVMe e na nova imagem têm
o mesmo hash `ecc69542ef20dbebe5964e79f6b4d55bcbc82702849611093e5aa940fe811fc3`.
O código de seleção da câmera do monitor permaneceu no índice 0. Apenas
`logs/` foi incluído no `.dockerignore` da pose para não enviar os logs ao
contexto de build. Os dois modelos ONNX da imagem carregaram no ONNX Runtime.

Os arquivos originais substituídos foram copiados para
`/home/bruno/pi-nvme-backup-20261002.LGK9rj`, fora do repositório. Há
manifesto SHA-256 verificado nessa pasta. O retrato posterior da máquina está
em `post-deploy/` na mesma pasta.

## Testes realizados

- Os 22 testes selecionados de roteamento, supervisor, CLI e extração passaram
  **dentro da imagem FATIGUE** (`python3 -m unittest`, 22/22). A seleção exige
  uma câmera distinta do índice 0 e recusa ausência ou ambiguidade.
- A primeira partida do `monitor-motorista` teve `Camera frontend has timed
  out!`. Após uma nova partida, abriu a IMX500 em `i2c@70000`, índice 0, e
  processou ao menos 3.030 quadros, aproximadamente 12–13 FPS de processamento,
  sem reinício nesse intervalo. O teste de captura no host com os controles
  usados pelo monitor também obteve um quadro.
- Nos logs do monitor, `Face: NO` durante a amostra. Isso comprova captura e
  processamento, mas **não** valida estimativa de pose de um rosto. O estado
  exibido foi `ATENTO` mesmo sem face; não deve ser interpretado como
  confirmação de atenção do operador.
- O FATIGUE está em execução como supervisor, com zero reinícios, aguardando
  a única câmera além do índice 0. Não abriu a câmera do monitor nem executou
  inferência ocular neste ensaio. O arquivo
  `/mnt/nvme/Monitoramento/FATIGUE/config/eye_calibration.json` está ausente;
  calibração ocular permanece pendente.

## Bloqueios observados

`rpicam-hello --list-cameras` enumera somente a IMX500 de `i2c@70000`.
`0-001a` e `0-0040` têm drivers vinculados; `10-001a` e `10-0040` não têm.
Não houve nova tentativa de probe ou teste elétrico nesta implantação. Com a
reserva atual do índice 0 ao monitor, a câmera que falta para o FATIGUE é a
de `i2c@88000`. Essa atribuição operacional segue a escolha atual do usuário;
documentos históricos de 25/09 registram a intenção inversa por porta física.

O kernel registrou timeouts de leitura e escrita do NVMe durante os builds e
novamente às 16:32:49 -03, enquanto os containers estavam ativos. As imagens
foram construídas, importadas e os arquivos conferidos, mas o armazenamento
**não passou** em validação de estabilidade para operação contínua. Esses
timeouts não demonstram a causa da falha da segunda câmera.

Os containers `monitor-motorista` e `salte-fatigue` estavam `running`, com
zero reinícios, no retrato das 16:34:12 -03. Esse estado é pontual e não
substitui nova verificação antes de uma operação real. Não houve sudo,
reinício, rebind de driver nem alteração de boot/SSH/VPN.
