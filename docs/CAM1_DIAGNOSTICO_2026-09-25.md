# Diagnóstico da CAM1 sem desligar o Pi — 2026-09-25

> **Correção de nomenclatura em 2026-09-25:** este registro preserva os nomes
> usados durante o teste, mas eles estavam invertidos em relação à serigrafia
> da CM5IO oficial. `i2c@88000`/barramento 10 é **CAM/DISP0** (ponte com erro
> `-121`); `i2c@70000`/barramento 0 é **CAM/DISP1** (captura funcional).
> Todas as referências a CAM0/CAM1 abaixo devem ser lidas com essa inversão.
> O teste do regulador e seus resultados continuam válidos para os barramentos
> explicitados.

## Escopo

Pi `raspberrypi5@192.168.15.62`. CAM0 é a IMX500 em `i2c@70000`, reservada ao detector de pose; CAM1 é a IMX500 em `i2c@88000`, reservada ao FATIGUE ocular. Nenhum container foi iniciado, nenhuma imagem foi enviada aos modelos, nenhum vídeo foi salvo, nenhum cabo foi manipulado e não houve reinício. As consultas ao Pi e o único teste ativo foram feitos por SSH; este registro está no PC.

## Estado inicial — 14:41:49 -03:00

- `rpicam-hello --list-cameras` mostrou somente a IMX500 em `i2c@70000`.
- `10-0040` (`rp2040-gpio-bridge`) e `10-001a` (`imx500`) existiam no barramento `i2c-10`, correspondente a `i2c@88000`, mas ambos estavam **sem link `driver`**.
- O regulador `cam0_reg` estava `disabled`, com `num_users=0`.
- O kernel já registrava `Could not get device info` e `probe ... failed with error -121` para `10-0040` no boot, às 13:22:31, e em outra tentativa anterior, às 13:29:59.

Trecho da enumeração e dos vínculos antes do teste:

```text
10-0040 name=rp2040-gpio-bridge driver=UNBOUND
10-001a name=imx500 driver=UNBOUND
cam0_reg state=disabled num_users=0
0 : imx500 [4056x3040 10-bit RGGB] (/base/axi/pcie@1000120000/rp1/i2c@70000/imx500@1a)
rp2040-gpio-bridge 10-0040: Could not get device info
rp2040-gpio-bridge 10-0040: probe with driver rp2040-gpio-bridge failed with error -121
```

## Teste único com o regulador sustentado

1. `rpicam-hello --camera 0 --nopreview -t 45000` iniciou captura da IMX500 física em `i2c@70000`, sem saída de vídeo. O processo terminou com código 0.
2. Às 14:42:40, durante a captura, `cam0_reg` estava `enabled`, com `num_users=2`; `10-0040` ainda estava sem driver.
3. Às 14:43:01, com a captura ativa e o regulador confirmado em `enabled`, foi feita **uma** escrita de `10-0040` em `/sys/bus/i2c/drivers/rp2040-gpio-bridge/bind`. A escrita retornou erro de I/O (`rc=1`). O kernel registrou novamente `Could not get device info` e `probe with driver rp2040-gpio-bridge failed with error -121` no mesmo instante.
4. Imediatamente depois, `10-0040` e `10-001a` continuavam sem driver. `cam0_reg` ainda estava `enabled`, com `num_users=2`.
5. Às 14:43:33, a captura já havia terminado. `cam0_reg` voltou a `disabled`, `num_users=0`; `rpicam-hello --list-cameras` ainda mostrava somente a CAM0 em `i2c@70000`. O Pi seguia ligado, com uptime de 1 h 21 min.

Resultado imediato da tentativa:

```text
TIME 2026-09-25T14:43:01-03:00
BIND_RC 1
10-0040 DRIVER UNBOUND
10-001a DRIVER UNBOUND
REGULATOR enabled
USERS 2
rp2040-gpio-bridge 10-0040: Could not get device info
rp2040-gpio-bridge 10-0040: probe with driver rp2040-gpio-bridge failed with error -121
```

## Conclusão

A CAM1 continuou falhando na leitura da identificação da ponte RP2040 por I²C mesmo com a CAM0 capturando e o regulador compartilhado habilitado. O teste reduz a plausibilidade de uma falha explicada apenas pelo atraso de acionamento desse regulador. A falha permanece antes da camada de vídeo; cabo, contatos, conector, porta e eletrônica do módulo seguem como candidatos. Este teste não distingue qual deles está defeituoso. A CAM1 não chegou a enumerar, portanto não foi tentada captura nela.

Nenhuma alteração permanente de boot ou do projeto foi aplicada para alterar o comportamento das câmeras. Qualquer inspeção física deve esperar o equipamento ser desligado.
