# Part 6 - System design (about one page)

Bullets, tables and a simple diagram (ASCII or Mermaid) are welcome. Say what you would **assume** when the statement is unclear - we care about your reasoning, not a "right" answer. You will present this in the interview.

## Scenario

A diagnostics lab runs immunoassays on **96-well plates**.

* A liquid-handling **robot** prepares each plate from a **patient list** (sample id -> well) that an operator provides.
* A **plate reader**, controlled by vendor software on a **Windows PC**, measures every well and, when the read finishes, **writes a results file into a local folder** (CSV/XML; it can take several seconds to write, and the vendor software sometimes re-exports the same file).
* The lab works on a **local network, often without Internet**. Later, sites may sync to a cloud.
* Three kinds of users: **operators** (log in, start a run, watch progress, read results), **lab supervisors** (review/approve results, export a report), and a remote **support team** who need structured logs from every site to diagnose problems.
* There are dozens of sites, each with its own PC. Data about patients is sensitive.

Build a system for this. We will start with one site.

## 1. Architecture

```
Leitor ─grava─► pasta ─► Ingestor ─────────┐
                                           ▼
Navegador ─HTTP─► Backend FastAPI ───────► SQLite
                   │  (+ módulo de análise)
                   ├─► Adaptador do robô ─► Robô
                   └─► Logs JSON ┄(quando houver internet)┄► Nuvem/suporte
```

Tudo roda localmente em um PC Windows por site e funciona sem internet; assumo que o backend fica no mesmo PC do leitor e que os navegadores o acessam pela rede local. O ingestor é um serviço separado, para que um arquivo ruim não derrube a API: ele vigia a pasta do leitor e grava as leituras. O backend FastAPI cuida de login, runs, resultados, aprovação e relatório, e usa o módulo de análise (`analysis.py`, a lógica de cutoffs e chamadas do protótipo); o SQLite guarda tudo no site. O robô fica atrás de um adaptador, porque não sei como ele recebe a lista: sem integração, o sistema exporta a lista validada para importação manual. Os serviços iniciam com o Windows, e a nuvem fica para depois.

## 2. Data model

| Tabela | Campos principais |
|---|---|
| `user` | `user_id`, `role` (operator / supervisor / support), `password_hash` |
| `run` | `run_id` (UUID), `site_id`, `run_code` (único), `plate_barcode` (opcional), `status`, `created_by`, `created_at` |
| `sample` | `run_id`, `well`, `sample_id`, `sample_type` (único por `(run_id, well)`) |
| `result_file` | `file_id`, `run_id` (nulo até ser ligado), `sha256` (único), `status` |
| `reading` | `run_id`, `file_id`, `well`, `analyte`, `mfi_raw`, `mfi`, `bead_count` (único por `(run_id, well, analyte)`) |
| `call` / `run_cutoff` | resultados derivados, com `analysis_version` |
| `approval` / `event` | decisão do supervisor; trilha de auditoria |

A identidade da run é um `run_id` UUID gerado no site, porque um inteiro colidiria entre sites quando houver sincronização; o `run_code` é um apelido curto e único que o operador digita no software do leitor para ligar o arquivo à run, e o `plate_barcode` é opcional, pois nem toda placa tem código de barras. A identidade do result é `(run_id, well, analyte)`, com `UNIQUE` no banco, o que torna a ingestão idempotente, e o arquivo é identificado pelo `sha256` do conteúdo, e não pelo nome. Um arquivo sem par fica guardado com `run_id` nulo, e suas leituras só são criadas depois que ele é ligado a uma run. Assumo que o `sample_id` é um código, com o nome do paciente no sistema do hospital; as senhas ficam com hash, o acesso depende do papel, o suporte vê apenas logs sem dados de pacientes e toda leitura ou aprovação fica registrada em `event`.

## 3. API

| Método e caminho | O que faz |
|---|---|
| `POST /auth/login` | autentica e devolve um token |
| `POST /runs` | cria a run e devolve `run_id` e `run_code`, ou `422` com os erros por linha se a lista for inválida |
| `GET /runs?status=` | lista as runs por estado (painel) |
| `GET /runs/{id}` | estado e progresso (o front consulta a cada poucos segundos) |
| `GET /runs/{id}/worklist` | lista validada para o robô (manual ou pelo adaptador) |
| `GET /runs/{id}/results` | chamadas, cutoffs e `run_valid` (`409` se ainda não há leituras) |
| `POST /runs/{id}/approve` | supervisor aprova ou rejeita (`403` para outros papéis) |
| `GET /runs/{id}/report` | exporta o relatório (`409` se a run não foi aprovada) |

O front-end chama todos, exceto o `worklist`, que o adaptador do robô também usa. Não há endpoint para enviar o arquivo do leitor, porque ele chega pela pasta. Fora da tabela ficam `GET /files?status=unmatched` e `POST /files/{id}/link` (ligação manual de arquivo sem par), `POST /files/{id}/resolve` (o supervisor decide conflitos) e `GET /health`.

## 4. Ingesting the result file

O ingestor varre a pasta do leitor a cada uns 5 segundos e compara com o que já está no banco; escolhi a varredura, e não só eventos do sistema como o `watchdog`, porque eventos podem se perder sem aviso, e a varredura também cobre o PC que reiniciou. Como a gravação pode demorar, nenhum sinal basta para saber se o arquivo está completo, então combino três: tamanho e data iguais em duas varreduras seguidas, abertura sem bloqueio e estrutura válida (cabeçalho e número de linhas esperados); se não ficar válido em cerca de 2 minutos, vai para quarentena com alerta, e depois de estável eu o copio e processo apenas a cópia, para que o software do leitor possa sobrescrever o original sem afetar o que já recebemos. Para não processar duas vezes, o `sha256` do conteúdo é único em `result_file`, o que reconhece reexportações; `UNIQUE (run_id, well, analyte)` é a segunda barreira, e arquivo e leituras entram em uma única transação, sem resultado pela metade. Se o mesmo poço vier com valores diferentes em outro arquivo, é conflito para o supervisor, nunca sobrescrita silenciosa. Para ligar à run, uso o `run_code` que o operador digita no campo de nome da placa do software do leitor (assumo que ele tem esse campo, o que confirmaria com o fabricante) e confiro se os poços batem com as amostras da run; se faltar o código ou os poços divergirem, o arquivo fica "sem par" e o operador o liga manualmente, porque, na dúvida, prefiro não ligar a arriscar pôr o resultado de um paciente na run errada.

## 5. Failure modes

Como o backend roda no mesmo PC do leitor, um reinício derruba os dois ao mesmo tempo. Se ocorrer durante a gravação, o arquivo fica truncado, mas o ingestor não o processa (estrutura inválida): ele vai para quarentena com alerta, e o operador vê a run em "aguardando leitura", com a orientação de repetir a exportação. Se pegar a ingestão no meio, a transação única evita dado pela metade, e os serviços sobem sozinhos e varrem a pasta, reprocessando do zero.

Uma lista de pacientes com poço inválido ou repetido, ou `sample_id` vazio, é rejeitada por inteiro com `422` e os erros linha a linha, e nada vai ao robô; prefiro isso a aceitar só as linhas válidas, porque uma lista parcial parece completa e uma amostra descartada em silêncio faria o robô preparar a placa sem ela.

Se o suporte não alcança o site, os logs vão primeiro para o disco, com rotação e limite de tamanho, e seguem por uma fila local com nova tentativa quando a conexão voltar. Um número sequencial por site revela lacunas, e um heartbeat periódico distingue site offline de sistema quebrado; como último recurso, o operador gera um pacote de diagnóstico sem dados de pacientes.

## 6. Logging & telemetry

Cada passo é registrado quando acontece, como uma linha JSON com campos fixos: `ts` em UTC, `seq` sequencial por site, `site_id`, `level`, `component`, um `event` com nome estável (como `file.quarantined`), `run_id` e `run_code`, `file_id` e `sha256` quando houver arquivo, `user_id`, `request_id`, `outcome`, `error_code` e as versões do software e das regras de análise. Filtrando por site e run e ordenando por `seq`, o suporte reconstrói a história da run, por exemplo "arquivo válido, mas sem par porque veio sem o código", e uma lacuna no `seq` mostra logs faltando, o que importa porque o relógio dos PCs pode estar errado. Nunca registro nomes, senhas, tokens nem o conteúdo dos arquivos, só ids e contagens. A auditoria fica na tabela `event`, e um heartbeat leva versão, espaço em disco, hora da última varredura e tamanho da fila de envio.

## 7. Two weeks, one engineer

Priorizo pelo risco, e o maior risco está na integração com o leitor, cujo comportamento desconheço, e não nas telas. Construo primeiro um esqueleto fino que percorre o fluxo inteiro (lista de pacientes, arquivo do leitor, análise, resultado), para que os problemas de integração apareçam na primeira semana. No primeiro dia, peço ao fabricante um arquivo de exemplo real e a descrição de como o software grava e reexporta, e peço ao laboratório as regras de análise (cutoff e validade da leitura), que o enunciado não especifica, isolando-as em um módulo. Na primeira semana, faço o SQLite, a criação de runs com validação da lista e exportação manual, o ingestor completo, o módulo de análise e os logs JSON desde o começo. Na segunda, entram login e papéis, aprovação, relatório em CSV, uma tela mínima, a fila de arquivos sem par, os serviços do Windows e testes com arquivos reais.

Deixo de fora a sincronização com a nuvem (os UUIDs já deixam a porta aberta), a integração automática com o robô, a ligação automática por adivinhação, a atualização em tempo real e o relatório em PDF; se o prazo apertar, corto primeiro o acabamento da tela, nunca o ingestor nem a ligação segura à run. O primeiro teste é a ingestão, por ser a parte de maior risco: gravar o arquivo aos poucos e confirmar que nada é processado antes de completo, colocar o mesmo arquivo duas vezes e confirmar que as leituras não duplicam, e interromper o processo no meio e confirmar que o banco não fica com resultado pela metade.