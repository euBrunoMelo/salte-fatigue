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
A ferramenta histórica `calibrate_eyes.py` usa o mesmo seletor físico, mas não é
necessária para a operação nem incluída na imagem Docker.

Os registros `run-start.json` incluem `camera_id`, `camera_id_contains` e o
`camera_num` efetivamente aberto, `closure_method=absolute_ear` e o limiar usado.
O runtime não lê `config/eye_calibration.json`: cada olho fecha quando
**EAR <= 0,17**, configurável por `--closed-ear-threshold`.

Com dois olhos válidos, os estados aparecem como **ativo**, **fadiga** e **sono**:

| Rótulo | Código preservado | Critério da FSM |
|---|---|---|
| ativo | `safe` | Sem evidência de fadiga |
| fadiga | `warning` | Fechamento > 400 ms ou PERCLOS disponível >= 15% |
| sono | `critical` | Fechamento binocular contínuo >= 1 s ou PERCLOS disponível >= 30% |
| unknown | `unknown` | Observação indisponível ou insuficiente |

Sono tem prioridade e exige dois olhos válidos; um olho permite no máximo
fadiga. A duração geral pode continuar com um olho válido, mas o trecho
binocular só começa quando ambos os olhos válidos estão individualmente
fechados. Perder um olho interrompe esse trecho; recuperar os dois exige um
novo trecho contínuo de 1 s para evidência crítica por fechamento. Os trechos
não são somados e o limiar segue `--prolonged-closure-ms` quando configurado.
A recuperação de sono exige 500 ms contínuos sem evidência crítica: o estado
`critical` pode permanecer durante a histerese sem nova evidência binocular.
Intervalos entre observações maiores que 0,5 s interrompem o episódio ocular e
a contagem de recuperação, sem tratar a pausa como olhos fechados.
O PERCLOS mantém janela de 60 s, cobertura binocular mínima de 80% e exclusão
de piscadas até 400 ms. A FSM usa a métrica somente quando seu
`observation_state` é `ready` e seu valor não é nulo. PERCLOS disponível no
limiar crítico configurado permite promoção imediata com dois olhos atuais
válidos, sem esperar outro segundo de fechamento.

Nos logs, `closure_ms` mantém a duração geral e `closure_valid_eye_count`
mantém o mínimo histórico de olhos válidos durante o fechamento; esse mínimo
não representa a qualidade atual nem limita a promoção crítica.
`closure_binocular_ms` e `closure_binocular_prolonged` descrevem o trecho
binocular atual. Na reabertura válida, somente o evento correspondente
transporta o trecho encerrado; no próximo frame aberto esses campos voltam
a zero/`false`. Invalidez ou gap impedem finalização através da interrupção.
`fatigue_label` apresenta o rótulo;
`perclos_ear` contém o PERCLOS absoluto e `perclos_p80` permanece `null`.
Pose bruta continua nos logs; opções dependentes de pose neutra foram retiradas.
A FSM temporal EAR/PERCLOS foi mantida, sem adotar três faixas diretas de EAR.

Para atualizar o FATIGUE no Raspberry, verificar antes os erros do NVMe e guardar
os arquivos e a imagem anteriores. Timeouts de armazenamento ou bloqueios de
escrita impedem o build e a implantação remotos.

```bash
docker compose config --quiet
docker compose build salte-fatigue
docker compose up -d --no-deps --force-recreate salte-fatigue
docker logs --follow --tail 100 salte-fatigue
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
