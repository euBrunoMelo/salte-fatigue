# CM5IO: câmeras e implantação do FATIGUE — 2026-09-25

## Identificação das portas

O equipamento usa Compute Module 5 na CM5IO oficial. Os símbolos do Device
Tree efetivamente carregado e o overlay IMX500 do kernel 6.12 associam:

| Porta da CM5IO | I²C | Sensor/ponte | CSI | Situação neste boot |
| --- | --- | --- | --- | --- |
| CAM/DISP0, pose | `i2c@88000`, adaptador 10 | `10-001a` / `10-0040` | CSI0 | Ponte sem driver; leitura da identificação falha com `-121` |
| CAM/DISP1, FATIGUE ocular | `i2c@70000`, adaptador 0 | `0-001a` / `0-0040` | CSI1 | Ambos os drivers vinculados; captura funcional |

Os GPIOs de I²C e os caminhos CSI/clock são próprios de cada instância no
Device Tree ativo. Ambas recebem `cam0_reg`, compartilhamento previsto para
a CM5IO. O acesso `fast_xfer` da ponte acontece depois da leitura de
identificação que falha; não explica esse erro inicial. Kernel, módulos,
firmware e overlays instalados passaram na verificação de integridade dos
pacotes. Não há causa definitiva demonstrada para `-121`.

Os documentos históricos `CAM1_DIAGNOSTICO_2026-09-25.md` e
`VALIDACAO_ENVIO.md` usaram CAM0/CAM1 ao contrário. Os IDs de barramento e
os resultados medidos neles foram preservados; a errata no início de cada
documento esclarece a inversão. O usuário confirmou a associação pela
serigrafia oficial: pose na CAM/DISP0, FATIGUE na CAM/DISP1.

Referências: [overlay IMX500 Pi 5](https://raw.githubusercontent.com/raspberrypi/linux/rpi-6.12.y/arch/arm/boot/dts/overlays/imx500-pi5-overlay.dts),
[documentação CM5IO](https://www.raspberrypi.com/documentation/computers/compute-module.html).

## Implantação

- Boot ID: `60c10e5b-5a2f-4df5-a402-1b51dd076fd1`; nenhum reboot durante
  esta implantação.
- Fonte verificada por `docs/RELEASE_MANIFEST_2026-09-25.sha256` e enviada a
  `/home/raspberrypi5/salte-fatigue-staging-20260925` no armazenamento do
  sistema. A suíte local passou com 127 testes.
- O daemon original tem dados em `/mnt/nvme/docker`; o `containerd` original
  usa `/mnt/nvme/containerd`. Como o NVMe continua com timeouts, a imagem
  nova foi construída em daemon e `containerd` temporários separados no SD.
  Socket: `/run/salte-fatigue-docker/docker.sock`. Dados:
  `/var/lib/salte-fatigue-docker` e `/var/lib/salte-fatigue-containerd`.
- Imagem `salte-fatigue:cm5io-20260925` e `salte-fatigue:latest`, digest
  `sha256:e1b1b4ceb167978604e11205d6da1319e0517c5dd5a6c9c61b893b46bc04a849`.
  Serviço Compose `salte-fatigue-isolated`, `network_mode: host`, política
  `unless-stopped`, sem gravação de incidentes.
- Container `salte-fatigue` iniciado às `2026-09-25T19:19:22Z`. O log
  identificou `1f00070000.i2c/i2c-0/0-001a`, registrou 2.406 frames depois
  de cerca de 80 segundos e não registrou reinícios. Sem calibração ocular,
  o estado é `unknown`, como previsto. Logs estão no staging do SD. Havia
  cerca de 16 GB livres no armazenamento do sistema após o build.

Consultar estado e logs:

```bash
sudo docker -H unix:///run/salte-fatigue-docker/docker.sock ps
sudo docker -H unix:///run/salte-fatigue-docker/docker.sock logs --tail 50 salte-fatigue
systemctl status salte-fatigue-containerd salte-fatigue-dockerd-sd
```

As unidades são transitórias e não iniciam automaticamente após um reboot.
O FATIGUE aguarda a ID física `70000` caso a câmera designada desapareça;
nenhuma imagem de `88000` é enviada ao modelo ocular. O patch local do detector
de pose agora exige `88000` e ainda não foi implantado no projeto de pose.
