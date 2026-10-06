# CAM/DISP0: investigação do driver — 2026-10-02

## Estado observado diretamente

No CM5 `raspberrypi`, boot ID `0a2b9133-9d73-474c-b524-333d30052010`,
kernel `6.12.75+rpt-rpi-2712`, somente a IMX500 de `i2c@70000` enumera.
`10-0040` (`rp2040-gpio-bridge`) e `10-001a` (`imx500`) da CAM/DISP0,
`i2c@88000`, estão declarados no Device Tree, mas sem driver vinculado. O
journal deste boot contém uma falha da ponte às 16:46:56 -03 de 25/09:
`Could not get device info`, seguida de `probe ... failed with error -121`.
As leituras de 02/10 não repetiram o probe.

O módulo carregado e o arquivo em disco informam o mesmo `srcversion`,
`715A3164EE83BB4183C4207`; `dpkg --verify` do pacote
`linux-image-6.12.75+rpt-rpi-2712` não produziu divergências. O pacote
instalado é `1:6.12.75-1+rpt1`. Seu [pacote-fonte oficial](https://archive.raspberrypi.com/debian/pool/main/l/linux/linux_6.12.75-1+rpt1.debian.tar.xz)
contém o driver em `debian/patches/rpi/rpi.patch`: o probe obtém e habilita
`power-supply`, faz `i2c_master_recv()` de 32 bytes, repete apenas
`-ETIMEDOUT`, e no erro desabilita runtime PM e libera sua referência ao
regulador. A falha observada aconteceu antes da identificação e da leitura
da versão do firmware da ponte problemática. O aviso `fast_xfer-gpios` da
ponte funcional ocorre depois da identificação dela e não explica essa falha.

O Device Tree carregado atribui `power-supply` de ambas as pontes ao
`cam0_reg`, comandado pelo GPIO34 com `startup-delay-us=300000`, e configura
`i2c@88000` a 100 kHz. Durante a coleta, com o monitor em execução, o
regulador estava `enabled` com dois usuários, GPIO34 alto e GPIO38/39 altos
em repouso. Esses estados não medem a tensão na câmera nem mostram o
sequenciamento elétrico durante o probe de 25/09.

No código do controlador [DesignWare I²C](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/drivers/i2c/busses/i2c-designware-common.c),
`-EREMOTEIO` (`-121`) pode corresponder a ausência de ACK ou SDA presa em
nível baixo. O motivo detalhado do abort daquela transação não foi preservado
no journal. Não há evidência suficiente para classificar o problema como
defeito do binário do driver, cabo, câmera, conector ou placa.

## Outros problemas no mesmo equipamento

Às 17:07 -03 de 02/10, o CM5 estava a 84 °C com `get_throttled=0xe0008`:
limite térmico ativo e eventos prévios de redução de frequência. O NVMe
estava a aproximadamente 51 °C e o kernel seguia registrando timeouts de
escrita até pelo menos 17:06. Esses problemas precisam de investigação
própria; não há medição que os ligue ao `-121` do primeiro probe em 25/09.
O `monitor-motorista` estava em execução com a câmera funcional, e o
`salte-fatigue` aguardava a segunda câmera.

## Próximo ensaio remoto, após janela autorizada

1. Registrar horário, temperatura CM5/NVMe, `get_throttled`, contagem de
   timeouts NVMe na janela anterior e estado dos dois containers.
2. Parar somente `monitor-motorista` por 10 minutos, mantendo FATIGUE em
   espera. Amostrar temperaturas a cada 2 minutos e contar novos timeouts
   de escrita do NVMe; registrar eventual atividade não relacionada.
3. Reiniciar o mesmo container sem rebuild nem troca de câmera. Confirmar
   índice 0, captura, reinícios, temperatura e timeouts nos 10 minutos
   seguintes. Não interpretar queda de temperatura como prova de causa dos
   timeouts ou do erro `-121` sem repetição controlada.

Esse ensaio interrompe a captura do monitor e deve ocorrer somente após
autorização do operador. Não força nova sondagem I²C e não reinicia o Pi.

## Ensaio discriminante futuro, com acesso físico

Com configuração e cabos intocados, primeiro desligar e comprovar a
desenergização completa, ligar e registrar o **primeiro** probe. Se a porta
continuar falhando, comparar o mesmo conjunto câmera+cabo em
CAM/DISP1 → CAM/DISP0 → CAM/DISP1, com alimentação desligada entre mudanças
e apenas o overlay da porta ensaiada. Medir habilitação, alimentação e
SDA/SCL na CAM/DISP0 durante o probe. A [documentação da CM5IO](https://datasheets.raspberrypi.com/cm5/cm5io-datasheet.pdf)
descreve controle de desligamento específico nessa porta. Não alterar atraso,
forçar GPIO, usar `i2cset` ou trocar peças antes dessas observações.

## Evidências

Acervo exportado para o PC em
`/home/bruno/pi-driver-evidence-20261002-0a2b9133`, com log completo do
kernel, Device Tree, configuração, vínculos, versão do módulo, temperaturas,
pacote-fonte exato e `manifest.sha256` verificado. O boot ID não mudou
durante a coleta. Não houve `sudo`, rebind, reinício ou sondagem I²C nesta
etapa.
