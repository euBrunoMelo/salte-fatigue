# salte-fatigue

Runtime EAR/PERCLOS para Raspberry Pi e IMX500. A fonte Picamera2 padrão é a
câmera física do barramento **70000** (`i2c@70000` ou `70000.i2c`), selecionada
pelo ID libcamera. O índice pode mudar entre boots.

```bash
python3 run_host.py --camera-id-contains 70000 --no-danger-record
```

O processo abre essa câmera diretamente. Fonte ausente, ambígua ou ocupada causa
erro imediato; o Docker mantém sua política `restart: unless-stopped`.
O FATIGUE não depende de índices reservados pelo `monitor-motorista`.
`--camera-num` é o índice OpenCV quando se usa `--no-picamera`.
A ferramenta `calibrate_eyes.py` usa o mesmo seletor físico.

Os registros `run-start.json` incluem `camera_id`, `camera_id_contains` e o
`camera_num` efetivamente aberto. Sem calibração válida e sem fallback, a captura
continua e o detector permanece `UNKNOWN`.

Para atualizar o FATIGUE no Raspberry, verificar antes os erros do NVMe e guardar
os arquivos e a imagem anteriores. Timeouts de armazenamento ou bloqueios de
escrita impedem o build e a implantação remotos.

```bash
docker compose config
docker compose build salte-fatigue
docker compose up -d --no-deps salte-fatigue
```

A implantação altera somente o FATIGUE. DrowsyDriving, `monitor-motorista` e
`gallery` continuam com suas configurações e processos existentes.
O container antigo `salte-tev8` não integra esse teste.

Validar por dez minutos com os serviços simultâneos: frames e timestamps
avançando, rosto detectado, EAR binocular válido, segmentos de logs publicados,
continuidade do monitor e resposta da gallery. Reinícios, travamentos ou novos
timeouts de armazenamento impedem a aprovação da infraestrutura.

Testes locais, em um ambiente com as dependências do runtime:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
