# Plano de correções do projeto FATIGUE

Atualizado em 22/10/2026. Branch de trabalho: `melhoria-continua`.

Fonte histórica: [revisão técnica de 19/10/2026](REVISAO_PROJETO_2026-10-19.md), sobre a árvore de trabalho baseada em `37ad229ef76c8e9b4a7a71fe3e2556788dbda560`. Este plano registra a execução sem alterar a revisão ou suas evidências. Não foi localizado outro plano operacional de correções na árvore consultada.

## Estado das etapas

| Etapa | Objetivo | Achados e ordem de execução | Estado |
|---|---|---|---|
| 0 | Proteção do repositório e validação shell | F05 + F14 | **CONCLUÍDA** |
| 1 | Segurança funcional | F01 → (F03 + F13) → F02 → F04 | **PENDENTE** |
| 2 | Persistência e integridade | F06 → F07 → F08 | **PENDENTE** |
| 3 | Observabilidade | F09 → F10 | **PENDENTE** |
| 4 | Arquitetura | F12 → F11 | **PENDENTE** |

Os **14 achados originais** estão preservados: **1 HIGH, 7 MEDIUM e 6 LOW**. Dois estão resolvidos e 12 permanecem pendentes. A conclusão da Etapa 0 não autoriza a execução da Etapa 1.

## Etapa 0 — CONCLUÍDA

Commit confirmado no Git: **`9edcb7894b5e2c02c0006a33db490f55e4093528`** (`9edcb78`), `fix(ops): correct repository guards and shell validation`, de 22/10/2026. É o HEAD local de `melhoria-continua` nesta atualização. O commit contém exatamente:

| Arquivo | Alteração registrada no commit |
|---|---|
| `.gitignore` | Modificado |
| `ops/validate_shell.sh` | Criado |
| `tests/test_repository_guards.py` | Criado |
| `TESTE REGRECAO.txt` | Modificado |

O histórico da execução compreende planejamento, testes RED, implementação GREEN, validação independente, fortalecimento da cobertura e revisão final. O RED inicial executou 18 testes: 10 falhas esperadas e oito aprovações, reproduzindo F05 e F14. Após GREEN, a validação independente identificou lacunas de seleção dos scripts e de proteção integral de `__pycache__/`; três testes adicionais elevaram a cobertura específica a 21 testes.

A primeira revisão final identificou dois defeitos adicionais dentro deste escopo: influência de `CDPATH` na descoberta da raiz (MEDIUM) e fragilidade do teste de seleção com caminhos Unicode (LOW). Ambos foram reproduzidos e tratados por RED → GREEN. O teste de seleção passou a capturar argumentos separados por NUL, sem interpretar a saída de `bash -x`; a descoberta da raiz neutraliza `CDPATH` no `cd`. O teste adicional de CDPATH elevou a cobertura específica a 22 testes. Esses dois defeitos não recebem novos identificadores entre F01 e F14.

### Resultados históricos de encerramento

Estes resultados foram executados e informados nas fases anteriores da sessão; **não foram reexecutados nesta atualização documental**. O Git confirma o conteúdo e o commit, mas não comprova sozinho a execução dos testes.

| Verificação anterior | Resultado registrado |
|---|---|
| Testes específicos de F05/F14 | **22/22 aprovados** |
| Suíte canônica completa | **406/406 aprovados** |
| Validação shell individual | **7/7 scripts aprovados** |
| Mutações de omissão de scripts | Todas as sete omissões detectadas |
| Mutação de remoção de `__pycache__/` | Regressão detectada |
| Regressão de CDPATH | Aprovada após GREEN |
| Seleção em caminho Unicode com `LC_ALL=C` | Aprovada após correção do teste |
| Revisão independente final | **APROVADO; nenhum finding concreto** |
| Preservação da árvore na revisão final | 1.852 arquivos preexistentes preservados |

Comandos de referência para futuras validações. Nesta atualização documental, somente `git diff --check` foi executado; os testes e a validação shell abaixo não foram reexecutados:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -p 'test_repository_guards.py' -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
bash ops/validate_shell.sh
env -u CDPATH bash ops/validate_shell.sh
CDPATH=. bash ops/validate_shell.sh
git diff --check
```

O validador seleciona `ops/launch_codex_rpi.sh`, `ops/setup_codex_rpi_lan.sh`, `ops/setup_codex_rpi_ssh.sh`, `ops/validate_shell.sh`, `sync/install-monitoramento-sync.sh`, `sync/monitoramento-sync.sh` e `sync/99-monitoramento-sync`. Cada arquivo é analisado por `bash -n --`, sem execução de seu corpo; entradas inexistentes ou inválidas produzem falha agregada. Caminhos com espaços e Unicode são preservados.

**Publicação remota confirmada:** `9edcb78` foi publicado em `origin/melhoria-continua`. A branch `main` permaneceu em `b0d8acc`, tanto localmente quanto em `origin/main`. As referências foram confirmadas por consulta somente de leitura com `git ls-remote --heads` ao repositório de `origin`, via HTTPS, em 22/10/2026. **Nenhuma implantação no Raspberry comprovada.** A publicação não certifica o estado operacional implantado.

## Registro dos 14 achados

As severidades, evidências originais e localizações pertencem à revisão de 19/10/2026. As referências de linha daquela revisão são históricas e devem ser conferidas antes de cada alteração. Os testes propostos para achados pendentes são requisitos para o próximo planejamento, sem alegação de implementação ou aprovação. As dependências abaixo expressam o sequenciamento operacional aprovado.

### F01 — Classificação antiga permanece como safe após ausência de frames

- **Severidade:** HIGH. **Estado:** PENDENTE, Etapa 1. **Dependências:** Etapa 0 concluída; primeiro achado da próxima execução.
- **Arquivos envolvidos:** `run_host.py`, testes de captura e estado do runtime a selecionar no planejamento.
- **Evidência:** revisão original, F01: fonte OpenCV sem frames por 3.600 s manteve `fatigue_state='safe'`; a validade do snapshot não considera a idade da observação.
- **Testes de regressão a planejar:** frame válido seguido de ausência; relógio controlado além do prazo; exigir UNKNOWN/UNAVAILABLE, saúde de captura e recuperação. Cobrir OpenCV, Picamera2 e captura bloqueada sem depender de hardware ou espera real.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** estado antigo pode continuar exposto; recuperação e bloqueio real de libcamera exigirão validação operacional controlada.

### F02 — Inversão indevida dos canais BGR da câmera

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 1. **Dependências:** conclusão de F03 + F13, após F01.
- **Arquivos envolvidos:** `run_host.py`, `feature_extractor_rt.py`, testes da fronteira de câmera e extração.
- **Evidência:** revisão original, F02: pixel BGR `[10, 20, 200]` saiu como `[200, 20, 10]`; o formato `RGB888` do Picamera2 já fornece o array na ordem BGR utilizada pelo contrato.
- **Testes de regressão a planejar:** câmera falsa com canais distintos; verificar BGR na saída e uma única conversão para RGB na entrada do backend.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** impacto sobre a detecção facial e cores no IMX500 necessita HIL.

### F03 — Configurações temporais aceitam valores inválidos

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 1. **Dependências:** F01; tratar em conjunto com F13.
- **Arquivos envolvidos:** `run_host.py`, `eye_closure.py`, `perclos_rt.py`, `fatigue_fsm.py` e testes de configuração/CLI.
- **Evidência:** revisão original, F03: parâmetros NaN aceitos; após dois segundos de fechamento binocular o resultado permaneceu `safe`. Finitude, faixas e relações entre limiares não são validadas.
- **Testes de regressão a planejar:** NaN, infinitos, zero, negativos, cobertura fora da faixa, janela inválida e limiares invertidos; construção direta e CLI; bordas válidas; rejeição antes de abrir câmera ou escritores.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** configurações inválidas podem desativar vias de detecção; preservar as configurações operacionais válidas.

### F04 — Falhas no encerramento impedem limpeza dos recursos

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 1. **Dependências:** F02, após F01 e F03 + F13.
- **Arquivos envolvidos:** `run_host.py`, contratos de encerramento de câmera/backend, recorder, logger e socket; testes de ciclo de vida.
- **Evidência:** revisão original, F04: exceção em `backend.close()` impediu chamadas posteriores a recorder e logger.
- **Testes de regressão a planejar:** injetar falha em cada fechamento; exigir tentativa de limpeza dos recursos restantes, restauração do logging, preservação da causa original e respeito ao prazo global.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** drenagem incompleta e perda do diagnóstico original; encerramento deve permanecer limitado no tempo.

### F05 — Regras inválidas no .gitignore

- **Severidade:** MEDIUM. **Estado:** RESOLVIDO, Etapa 0 concluída. **Dependências:** nenhuma pendência bloqueante para o encerramento local.
- **Arquivos envolvidos:** `.gitignore`, `tests/test_repository_guards.py`.
- **Evidências:** revisão original, F05; commit `9edcb78`; regras sem heredoc/indentação indevida; proteção de ambientes, caches, logs, calibração e vídeos gerados; exceções para exemplos, evidências e fixtures. Nenhuma regra específica para `.claude/` foi adicionada.
- **Testes de regressão:** casos positivos e negativos com Git real em repositórios temporários, precedência das exceções, proteção integral de `__pycache__/` e preservação do índice temporário. Integram os 22 testes específicos aprovados; remoção isolada de `__pycache__/` foi detectada por mutação.
- **Commit de resolução:** `9edcb78`. **Resultado da revisão:** APROVADO, nenhum finding concreto. **Riscos residuais:** os 29 arquivos previamente rastreados continuam no índice; regras de ignore não retiram arquivos já versionados.

### F06 — Falha na leitura do kernel interpretada como zero timeouts

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 2. **Dependências:** Etapa 1 concluída; primeiro achado da Etapa 2.
- **Arquivos envolvidos:** `ops/collect_pi_health.py` e testes de coleta/saúde a definir.
- **Evidência:** revisão original, F06: erro de permissão em `dmesg` produziu `nvme_write_timeout_count=0`.
- **Testes de regressão a planejar:** leitura válida sem eventos, válida com eventos, permissão negada e timeout; distinguir zero conhecido de dado indisponível e explicitar a janela do contador.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** indisponibilidade da consulta pode parecer armazenamento saudável; resultado local não certifica a saúde do NVMe.

### F07 — Manifestos ausentes ou vazios considerados válidos

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 2. **Dependências:** F06.
- **Arquivos envolvidos:** `ops/nvme_trace_audit.py`, `ops/analyze_pi_nvme_episodes.py` e testes de integridade de manifestos.
- **Evidência:** revisão original, F07: ausência ou vazio de `SHA256SUMS` retornou `checked=0`, `failures=[]` e condição de validade verdadeira.
- **Testes de regressão a planejar:** manifesto ausente, vazio, incompleto, duplicatas conflitantes e válido; exigir cobertura dos insumos obrigatórios e impedir aprovação sem entradas verificadas.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** hashes calculados na reanálise não substituem a comparação com evidência preservada; não modificar evidências históricas para satisfazer a validação.

### F08 — Partials antigos não informados pelo logger assíncrono

- **Severidade:** MEDIUM. **Estado:** PENDENTE, Etapa 2. **Dependências:** F07.
- **Arquivos envolvidos:** `persistence_worker.py`, `async_structured_logger.py`, `structured_logger.py` como referência e testes de persistência/saúde.
- **Evidência:** revisão original, F08: partial de outro `run_id` foi preservado sem evento ou campo de saúde; a cobertura existente exercitava a classe síncrona.
- **Testes de regressão a planejar:** iniciar logger assíncrono com partial anterior; exigir aviso/saúde e preservação integral, sem promoção automática; limitar custo da auditoria fora da inferência.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** evidência de interrupção permanece invisível; varredura ampla pode comprometer inicialização e I/O.

### F09 — Estatísticas de sincronização incompletas

- **Severidade:** LOW. **Estado:** PENDENTE, Etapa 3. **Dependências:** Etapa 2 concluída; primeiro achado da Etapa 3.
- **Arquivos envolvidos:** `sync/monitoramento-sync.sh`, `tests/test_sync_contract.py` e contratos de status da sincronização.
- **Evidência:** revisão original, F09: primeira tentativa transferiu um arquivo de nove bytes e falhou; segunda concluiu sem novas transferências; status final informou totais zero.
- **Testes de regressão a planejar:** rsync falso com transferência parcial e erro seguido de sucesso; conferir totais e tentativas; definir se a métrica conta operações ou arquivos únicos.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** totais subestimados; estatística local não constitui ACK de recebimento no destino.

### F10 — Informações de exceções descartadas pelo logger

- **Severidade:** LOW. **Estado:** PENDENTE, Etapa 3. **Dependências:** F09.
- **Arquivos envolvidos:** `async_structured_logger.py`, chamadas em `run_host.py` e testes do console estruturado.
- **Evidência:** revisão original, F10: `LogRecord` com `exc_info` perdeu tipo, causa e contexto no payload.
- **Testes de regressão a planejar:** exceção com causa e stack; exigir tipo, mensagem e trecho útil do traceback no JSON, com limite de tamanho e preservação da política assíncrona de capacidade.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** diagnóstico incompleto; serialização deve respeitar limites de memória e do canal.

### F11 — Módulos extensos com responsabilidades concentradas

- **Severidade:** LOW. **Estado:** PENDENTE, Etapa 4. **Dependências:** F12; executar depois dos contratos explícitos.
- **Arquivos envolvidos:** `run_host.py`, `ops/analyze_pi_io_diagnostics.py`, `ops/analyze_pi_nvme_episodes.py`, `ops/analyze_pi_thermal.py`, `ops/collect_pi_inventory.py`, `ops/pi_io_probe.py`, `ops/render_pi_inventory.py` e `tests/test_deterministic_runtime.py`.
- **Evidência:** revisão original, F11: sete módulos ativos excediam 500 linhas; captura, CLI, ciclo de vida e HUD concentrados no host, e cálculo/agregação/apresentação nos analisadores.
- **Testes de regressão a planejar:** caracterizar contratos antes das extrações, manter a suíte canônica e testar unidades extraídas; verificar tamanho/profundidade segundo a política aplicável, sem impor preferências de formatação.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** refatoração ampla pode alterar interfaces e evidências; preservar snapshots históricos e dividir a execução por responsabilidade.

### F12 — Contratos de persistência sem tipos explícitos

- **Severidade:** LOW. **Estado:** PENDENTE, Etapa 4. **Dependências:** Etapa 3 concluída; precede F11.
- **Arquivos envolvidos:** `async_danger_recorder.py`, `async_structured_logger.py`, `persistence_transport.py`, `recorder_persistence_worker.py`, produtores/consumidores adjacentes e testes de contratos.
- **Evidência:** revisão original, F12: mensagens críticas e snapshots usam dicionários com chaves implícitas; auditoria AST encontrou pelo menos 305 de 505 funções com anotações incompletas.
- **Testes de regressão a planejar:** serialização dos comandos, validação de mensagens incompletas na fronteira e checagem de tipos dos produtores/consumidores; definir ferramenta e alcance antes da implementação.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** incompatibilidade de esquema entre processos; preservar o protocolo de transporte e distinguir rejeição de contrato de falha real de armazenamento.

### F13 — Defaults temporais duplicados na CLI

- **Severidade:** LOW. **Estado:** PENDENTE, Etapa 1. **Dependências:** F01; tratar em conjunto com F03.
- **Arquivos envolvidos:** `run_host.py`, `eye_closure.py`, `perclos_rt.py`, `fatigue_fsm.py`, composição de `FatigueRuntimeConfig` e testes de CLI/configuração.
- **Evidência:** revisão original, F13: defaults repetidos no parser e nas configurações; alterar somente uma dataclass não muda o default operacional.
- **Testes de regressão a planejar:** comparar defaults do parser com `FatigueRuntimeConfig()` e conferir cada substituição explícita; preservar a ligação entre exclusão de piscadas e PERCLOS.
- **Commit de resolução:** não registrado. **Resultado da revisão de correção:** pendente. **Riscos residuais:** divergência entre configuração central e CLI; coordenar os testes com as novas validações de F03.

### F14 — Validação incompleta da sintaxe dos scripts shell

- **Severidade:** LOW. **Estado:** RESOLVIDO, Etapa 0 concluída. **Dependências:** nenhuma pendência bloqueante para o encerramento local.
- **Arquivos envolvidos:** `ops/validate_shell.sh`, `TESTE REGRECAO.txt`, `tests/test_repository_guards.py`.
- **Evidências:** revisão original, F14; commit `9edcb78`; comando canônico atualizado para `bash ops/validate_shell.sh`; análise individual substitui a chamada que verificava somente o primeiro arquivo.
- **Testes de regressão:** seleção dos sete obrigatórios, erro em cada posição, múltiplas falhas, ausência de arquivo, espaços, Unicode, CDPATH e não execução dos corpos. Integram os 22 testes específicos aprovados; as sete mutações de omissão foram detectadas.
- **Commit de resolução:** `9edcb78`. **Resultado da revisão:** APROVADO, nenhum finding concreto. **Riscos residuais:** seleção estática exige manutenção para novos scripts; análise sintática não comprova comportamento operacional.

## Pendências residuais da Etapa 0

São limitações e tarefas de acompanhamento, **não novos achados da revisão original nem reabertura automática de F05/F14**.

| Pendência | Evidência e tratamento |
|---|---|
| 29 arquivos rastreados correspondem ao `.gitignore` | Confirmado nesta atualização com `git ls-files --cached --ignored --exclude-standard`: `.ai-jail`, 18 arquivos `.pyc` na raiz `__pycache__/` e dez em `tests/__pycache__/`. Permanecem no índice e no histórico; qualquer saneamento requer escopo e autorização próprios. |
| Lista estática do validador | Sete entradas explícitas confirmadas no commit. Ao adicionar/remover scripts obrigatórios, atualizar o validador e seu contrato de testes conjuntamente. |
| ShellCheck e shfmt indisponíveis | Não executados nas fases anteriores; `shutil.which()` confirmou nesta atualização que ambos continuam ausentes no PATH. A validação com Bash não substitui esses verificadores. |
| Validação operacional no Raspberry | PENDENTE. Não há evidência de implantação deste commit; HIL e equivalência entre imagem implantada e fonte precisam de execução autorizada. |

## Processo obrigatório das próximas etapas

Para cada achado ou conjunto aprovado, seguir a sequência:

1. **`agentic-test-engineering` — planejamento e RED:** ler instruções e contratos, registrar a árvore inicial, confirmar a evidência, reutilizar testes pertinentes e obter reprodução determinística do defeito em ambiente isolado. Submeter o plano e respeitar a autorização específica de execução.
2. **`agentic-code-implementation` — GREEN:** implementar a menor correção autorizada; preservar testes, alterações preexistentes, contratos operacionais e evidências históricas.
3. **`agentic-test-engineering` — validação e regressões:** executar testes específicos e suíte canônica, verificar cobertura por comportamento e mutações quando pertinentes, registrar comandos/resultados e limitações.
4. **`agentic-code-review` — revisão independente:** incluir arquivos novos/não rastreados, analisar diff e contratos, classificar findings concretos e registrar o veredito. Havendo defeito bloqueante, retornar ao ciclo correspondente.
5. **Commit isolado após aprovação:** conferir o escopo, preservar mudanças alheias e registrar o hash de resolução com autorização para commit. Commit não implica push, implantação ou validação operacional.

Atualizar o registro do achado com identificador, severidade original, descrição, estado, dependências, evidências, arquivos envolvidos, testes de regressão, commit de resolução, resultado da revisão e riscos residuais. Distinguir resultados históricos dos comandos efetivamente executados em cada sessão. **Não declarar conclusão sem evidência de implementação, testes e revisão.**

## Próxima execução

**Próximo achado: F01 (HIGH), Etapa 1 — segurança funcional.** Começar pelo **planejamento de testes com `agentic-test-engineering`**, revisando o contrato de validade da observação e a cobertura de captura/estado. Definir relógio controlado, fontes falsas e critérios de aprovação antes de autorizar RED e implementação. As demais correções seguem a ordem da tabela de etapas.

Esta atualização não inicia a Etapa 1, não cria testes RED e não altera código de produção. A autorização da próxima etapa será fornecida separadamente.

## Verificações desta atualização documental

- Lidos `AGENTS.md`, `CLAUDE.md`, revisão original e protocolo/arquivos pertinentes ao commit. Nenhum plano operacional anterior foi localizado pelos nomes de arquivo e pela busca textual de etapas/correções fora das evidências históricas.
- `git show --format=fuller --stat --no-renames 9edcb78` e inspeção do diff completo: hash, mensagem e quatro arquivos confirmados. `git log -5` e `git status` confirmam HEAD/branch; não há commit posterior no histórico da branch consultada.
- `git diff 9edcb78 -- .gitignore ops/validate_shell.sh tests/test_repository_guards.py 'TESTE REGRECAO.txt'`: vazio; os quatro arquivos atuais correspondem ao commit.
- Conferidos os testes existentes por leitura, as sete entradas do validador, os 29 rastreados ignorados e a indisponibilidade de ShellCheck/shfmt. Nenhuma suíte, mutação ou validação shell foi executada nesta atualização.
- Estado inicial registrado: 1.852 arquivos fora de `.git`/`.venv`, 1.557 arquivos não rastreados e modificações preexistentes em `.ai-jail`, `README.md` e `ops/launch_codex_rpi.sh`.
- Conferência final: os 1.852 arquivos preexistentes mantiveram conteúdo, tamanho, modo e data; nenhum foi removido. A única adição é este plano, totalizando 1.853 arquivos e 1.558 não rastreados. Índice (bytes e entradas), HEAD e branch permanecem iguais ao estado inicial. O documento e o diff passaram na checagem de whitespace.
- Sem commit, alteração do índice, push, troca de branch, modificação de `main`, acesso ao Raspberry ou alteração de evidências históricas nesta sessão.
