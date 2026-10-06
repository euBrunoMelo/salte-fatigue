# Codex no Raspberry pela LAN ou VPN (Ubuntu 26.04)

Execute estes comandos **na raiz deste projeto, no terminal normal do PC
Ubuntu**, fora de qualquer `ai-jail`. Na sala, o acesso usa o Wi-Fi e o IP local
do Raspberry. Fora da sala, usa `raspberrypi5@100.120.192.2` pela interface `netmaker`. A chave
administrativa habitual fica no PC e não entra na jail.

## 1. Preparar os dois caminhos

Se a chave exclusiva e a configuração VPN ainda não existem, prepare-as uma
única vez com Netmaker ativo:

```bash
bash ops/setup_codex_rpi_ssh.sh
```

O script confere a rota da VPN e o `bubblewrap`, cria uma chave Ed25519 exclusiva
com senha em `~/.ssh/id_ed25519_codex_rpi_sala`, confirma a chave do servidor por
uma conexão SSH já confiável, instala somente a chave pública no Raspberry e
testa a chave nova sem usar o agente SSH do desktop. Ele não substitui uma chave
existente. Se a conexão administrativa requer uma identidade específica, execute:

```bash
RPI_ADMIN_KEY="$HOME/.ssh/SUA_CHAVE_ADMINISTRATIVA" bash ops/setup_codex_rpi_ssh.sh
```

Esse caminho só é usado na preparação, fora da jail. Se `bubblewrap` estiver
ausente no PC, instale o pacote `bubblewrap` do Ubuntu e repita a preparação.

Na sala, conectado ao mesmo Wi-Fi do Raspberry, prepare o caminho local:

```bash
bash ops/setup_codex_rpi_lan.sh
```

O IP local inicial é `192.168.15.62`, conforme os registros do projeto. Se ele
mudou, informe o endereço atual ao executar a preparação:

```bash
RPI_LAN_IP='IP_ATUAL_DO_RASPBERRY_NA_SALA' bash ops/setup_codex_rpi_lan.sh
```

Essa preparação exige rota direta pelo Wi-Fi, compara a chave SSH apresentada
na LAN com a chave do Raspberry já registrada para a VPN e testa a chave
exclusiva com um comando remoto de leitura. Se a identidade ou a conexão não
for confirmada, ela interrompe sem aceitar o novo endereço. Não reinstala a
chave pública no Raspberry nem precisa de uma conexão VPN ativa.

## 2. Conferir e iniciar

Na sala:

```bash
bash ops/launch_codex_rpi.sh --lan --check
bash ops/launch_codex_rpi.sh --lan --run
```

Fora da sala, com Netmaker ativo:

```bash
bash ops/launch_codex_rpi.sh --vpn --check
bash ops/launch_codex_rpi.sh --vpn --run
```

O primeiro comando mostra os mapeamentos sem iniciar o Codex. Confira que não
há mapeamento do `$HOME` inteiro, do `~/.ssh` real ou do agente SSH habitual.
O modo é obrigatório. `--lan` recusa uma rota que não seja direta pelo Wi-Fi;
ele não muda automaticamente para VPN. `--vpn` exige a rota `netmaker`.
Cada execução cria seu próprio `ssh-agent`; a chave expira nele após uma hora e
o agente termina ao sair do script. A senha da chave será solicitada em cada
execução. A jail recebe apenas a configuração do modo escolhido; dentro dela,
o alias SSH continua sendo `rpi-sala`.

O `ai-memory run --no-autowire codex` usa os hooks já instalados, sem precisar
do socket Docker. O Codex usa `danger-full-access` para executar comandos sem
criar um segundo sandbox dentro do `ai-jail`. A jail continua limitando os
arquivos e sockets visíveis, mas permite escrever no projeto e nos diretórios
explicitamente mapeados com `--rw-map`. O `--network` permite rede ampla; a
restrição do agente é para a identidade SSH do Raspberry, não para uma interface
de rede. Escolher `--lan` não desliga o Netmaker nem bloqueia a VPN para outros
processos.

O parâmetro `--ask-for-approval on-request` permanece, mas não oferece as
aprovações automáticas por saída do workspace que existiam com `workspace-write`:
o Codex pode executar comandos permitidos pelo `ai-jail` sem perguntar. Revise
as ações propostas antes de autorizar operações sensíveis, especialmente no
Raspberry. O filtro seccomp do `ai-jail` continua ativo; não é necessário
desligá-lo nem alterar o AppArmor ou os namespaces do Ubuntu.

## 3. Testar dentro do Codex

Peça primeiro somente estas verificações:

```bash
ssh -G -T -F /tmp/codex-rpi-ssh/config rpi-sala | awk '$1 == "hostname" { print $2; exit }'
SSH_AUTH_SOCK=/tmp/codex-rpi-agent.sock ssh-add -l
test ! -e "$HOME/.ssh/id_ed25519_codex_rpi_sala" && echo 'Chave privada ausente'
ssh -F /tmp/codex-rpi-ssh/config rpi-sala 'hostname; id -un; uptime'
```

O primeiro comando deve mostrar o IP local no modo `--lan` ou `100.120.192.2`
no modo `--vpn`. O agente deve listar uma identidade, a chave privada não deve
aparecer e o Raspberry deve responder como `raspberrypi5`. Se algum teste
falhar, interrompa as operações remotas e confira o erro antes de ampliar
permissões.

Para conferir a execução local e a escrita no projeto, rode em seguida:

```bash
bash -n ops/launch_codex_rpi.sh
bash -n ops/setup_codex_rpi_lan.sh
arquivo=$(mktemp -p . .codex-escrita-XXXXXX)
printf 'ok\n' > "$arquivo"
test "$(cat "$arquivo")" = ok
rm -- "$arquivo"
```

Esse teste cria e remove apenas um arquivo temporário no projeto. Ele deve
funcionar sem solicitar execução fora do sandbox interno do Codex.

O acesso remoto tem os poderes da conta `raspberrypi5`; o sandbox local não
limita o que essa conta pode fazer no Raspberry. Não use `sudo` nem comandos
destrutivos sem autorização explícita.
