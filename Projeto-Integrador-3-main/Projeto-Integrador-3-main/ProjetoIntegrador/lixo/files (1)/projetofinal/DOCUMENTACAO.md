# Documentação técnica — Projeto Integrador III

## 1. Arquitetura

API industrial (HTTP) → `coletor.py` (coleta a cada 5 s, valida, grava) → SQLite `dados.db` → servidor local (`/api/estado`, `/api/analise`, `/api/csv`) → `dashboard2.html`.

A API guarda dados só em memória e limpa a cada 60 s; por isso o coletor consulta a cada 5 s e persiste tudo.

## 2. Estrutura de armazenamento (dicionário de dados)

**Tabela `leituras`** — registros válidos.

| Campo | Tipo | Descrição |
|---|---|---|
| id | INTEGER | identificador enviado pela API |
| timestamp | TEXT | data/hora ISO 8601 (UTC) da leitura |
| maquina | TEXT | TORNO_CNC_01, PRENSA_HIDRAULICA_01, ESTEIRA_01, ROBO_SOLDAGEM_01, COMPRESSOR_01, EMBALADORA_01 |
| temperatura | REAL | °C |
| vibracao | REAL | mm/s (não vem em todas as máquinas) |
| pressao | REAL | bar (não vem em todas as máquinas) |
| producao | REAL | contador acumulado de unidades |
| status | TEXT | operando, parada, manutenção... |
| raw | TEXT | JSON original; `UNIQUE` evita duplicatas |
| coletado_em | TEXT | momento da gravação |

**Tabela `quarentena`** — registros rejeitados: `raw` (JSON), `motivo`, `coletado_em`.
Motivos: máquina ausente, timestamp ausente, campo numérico com tipo inválido, valor negativo (vibração, pressão, produção).

Relacionamento: a ligação entre máquinas é lógica (campo `maquina`) e pelo fluxo do processo (`FLUXO` em `coletor.py`). Não há chaves estrangeiras.

## 3. Métodos de análise

- **Produção:** soma das diferenças entre leituras do contador; queda do valor é tratada como reinício.
- **Disponibilidade** = tempo `operando` ÷ tempo observado (lacunas > 1 min são descartadas).
- **Perda estimada** = taxa média de produção enquanto operando × tempo parado.
- **Impacto logístico:** quando uma máquina para, listam-se as etapas seguintes em `FLUXO`. **A ordem do fluxo é uma suposição** e precisa ser confirmada.
- **Anomalias (IA estatística):** z-score = (valor − média) ÷ desvio padrão do histórico da máquina; |z| > 3 nas últimas 100 leituras (temperatura e vibração). Exige 30+ leituras.
- **Correlação:** coeficiente de Pearson (temperatura × vibração; temperatura × pressão).
- **Tendência:** regressão linear da temperatura nas últimas 120 leituras (°C/min).
- **Risco (0–100), heurística explicável:** 10 por anomalia + 15 por parada na última hora + 20 se parada/manutenção agora + 15 se temperatura subindo > 0,05 °C/min. Baixo < 30, médio < 60, alto ≥ 60.

**Limitações:** o z-score assume distribuição aproximadamente normal e usa o histórico inteiro (inclui as próprias anomalias); os pesos do risco são arbitrários, não treinados; correlação não indica causa; a perda estimada é uma aproximação; com pouco histórico os resultados são pouco confiáveis.

## 4. Segurança da informação

Implementado:
- Login (HTTP Basic) em todas as rotas; senha gerada aleatoriamente ao iniciar, se não informada (`--senha`).
- Comparação de credenciais em tempo constante (`hmac.compare_digest`).
- Servidor escuta só em `127.0.0.1` por padrão (`--host 0.0.0.0` libera na rede).
- CORS aberto removido; cabeçalhos `X-Content-Type-Options`, `X-Frame-Options` e `Content-Security-Policy`.
- Consultas SQL parametrizadas; escape de HTML nos dados exibidos.
- Validação de entrada e quarentena de registros inválidos (integridade).
- Tentativas negadas são registradas no terminal.

Limitações: Basic Auth trafega em texto puro sem HTTPS; usuário único, sem perfis; banco sem criptografia; sem limite de tentativas de login. Em produção: HTTPS, perfis de acesso e backup.

## 5. Como executar

```
py coletor.py                  # anote a senha mostrada no terminal
py coletor.py --senha minhasenha --base http://10.69.18.92:8080
```
Abra `http://localhost:5000` (não abra o HTML direto do arquivo, pois o login depende do servidor).

## 6. Esqueleto do relatório técnico (a escrever)

1. Introdução e problema 2. Arquitetura (seção 1) 3. Dados (seção 2) 4. Metodologia (seção 3) 5. Resultados com prints e cenários reais dos dados 6. Segurança (seção 4) 7. Limitações 8. Recomendações e conclusões 9. Relação com as unidades curriculares.
