"""Coletor independente do Dashboard 2.

Uso recomendado (Dashboard normal preservado na porta 5000):
    python coletor.py

Dashboard 2:
    http://localhost:5001

Diagnóstico:
    http://localhost:5001/api/diagnostico

A API industrial padrão e a rota /maquinas foram retiradas da documentação
htmlprojeto.html do projeto. Se o IP mudar, use --base para substituí-lo.
"""

import argparse
import csv
import io
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# ============================================================
# 1. CONFIGURAÇÕES
# ============================================================

Pasta = Path(__file__).resolve().parent

parser = argparse.ArgumentParser(
    description="Coletor e servidor exclusivo do Dashboard 2"
)
parser.add_argument(
    "--base",
    default="http://10.120.86.92:8080",
    help="Endereço base da API industrial"
)
parser.add_argument(
    "--rota",
    default="/maquinas",
    help="Rota que retorna as leituras das máquinas"
)
parser.add_argument(
    "--intervalo",
    type=float,
    default=5,
    help="Intervalo entre coletas, em segundos"
)
parser.add_argument(
    "--porta",
    type=int,
    default=5001,
    help="Porta local do Dashboard 2 (5001 para não conflitar com o normal)"
)
parser.add_argument(
    "--db",
    default="dados.db",
    help="Banco SQLite exclusivo do Dashboard 2"
)
parser.add_argument(
    "--timeout",
    type=float,
    default=10,
    help="Tempo limite da requisição à API, em segundos"
)
args = parser.parse_args()

if args.intervalo <= 0:
    parser.error("--intervalo precisa ser maior que zero")
if args.timeout <= 0:
    parser.error("--timeout precisa ser maior que zero")
if not (1 <= args.porta <= 65535):
    parser.error("--porta precisa estar entre 1 e 65535")

if not args.base.startswith(("http://", "https://")):
    parser.error("--base precisa começar com http:// ou https://")

URL_API = args.base.rstrip("/") + "/" + args.rota.lstrip("/")
ARQUIVO_HTML = Pasta / "dashboard2.html"

BANCO = Path(args.db)
if not BANCO.is_absolute():
    BANCO = Pasta / BANCO

CAMPOS = [
    "id", "timestamp", "maquina", "temperatura",
    "vibracao", "pressao", "producao", "status"
]

estado = {
    "ok": False,
    "ultimo_sucesso": None,
    "ultima_tentativa": None,
    "erro": "Aguardando a primeira coleta",
    "coletas": 0,
    "recebidas": 0,
    "novas": 0
}
lock_estado = threading.Lock()


def atualizar_estado(**valores):
    with lock_estado:
        estado.update(valores)


def obter_estado():
    with lock_estado:
        return dict(estado)


# ============================================================
# 2. SQLITE - MANTÉM O BANCO DADOS.DB DO DASHBOARD 2
# ============================================================

def conectar():
    con = sqlite3.connect(str(BANCO), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def criar_tabela():
    BANCO.parent.mkdir(parents=True, exist_ok=True)
    con = conectar()
    try:
        # Mantém a estrutura usada pelo coletor original.
        # CREATE IF NOT EXISTS não apaga nem recria os dados existentes.
        con.execute("""
            CREATE TABLE IF NOT EXISTS leituras (
                id INTEGER,
                timestamp TEXT,
                maquina TEXT,
                temperatura REAL,
                vibracao REAL,
                pressao REAL,
                producao REAL,
                status TEXT,
                raw TEXT,
                coletado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (raw)
            )
        """)
        con.commit()
    finally:
        con.close()


# ============================================================
# 3. LEITURA DA RESPOSTA DA API
# ============================================================

def extrair_leituras(dados):
    # Formato documentado: uma lista de objetos de máquinas.
    if isinstance(dados, list):
        return [item for item in dados if isinstance(item, dict)]

    if isinstance(dados, dict):
        # Aceita também respostas envelopadas, se a API usar esse formato.
        for chave in ("maquinas", "leituras", "dados", "data", "results"):
            valor = dados.get(chave)
            if isinstance(valor, list):
                return [item for item in valor if isinstance(item, dict)]

        # Aceita uma única leitura retornada como objeto.
        if any(campo in dados for campo in CAMPOS):
            return [dados]

    raise ValueError(
        "Formato JSON não reconhecido. Esperava uma lista de leituras "
        "ou um objeto contendo a lista."
    )


def consultar_api():
    requisicao = urllib.request.Request(
        URL_API,
        headers={
            "Accept": "application/json",
            "User-Agent": "Dashboard2-Coletor/1.0"
        },
        method="GET"
    )

    with urllib.request.urlopen(requisicao, timeout=args.timeout) as resposta:
        if resposta.status != 200:
            raise RuntimeError(f"A API retornou HTTP {resposta.status}")
        conteudo = resposta.read().decode("utf-8-sig")

    try:
        dados = json.loads(conteudo)
    except json.JSONDecodeError as erro:
        raise ValueError(f"A API não retornou JSON válido: {erro}") from erro

    return extrair_leituras(dados)


# ============================================================
# 4. GRAVAÇÃO DAS LEITURAS
# ============================================================

def salvar_leituras(dados):
    con = conectar()
    novas = 0
    try:
        for item in dados:
            valores = [item.get(campo) for campo in CAMPOS]
            raw = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True
            )
            cursor = con.execute("""
                INSERT OR IGNORE INTO leituras (
                    id, timestamp, maquina, temperatura,
                    vibracao, pressao, producao, status, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, valores + [raw])
            novas += max(cursor.rowcount, 0)
        con.commit()
        return novas
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ============================================================
# 5. COLETA AUTOMÁTICA COM MENSAGENS NO TERMINAL
# ============================================================

def loop_coleta():
    print("\n" + "=" * 65, flush=True)
    print("COLETOR DO DASHBOARD 2 - INICIADO", flush=True)
    print(f"API: {URL_API}", flush=True)
    print(f"Intervalo: {args.intervalo} segundo(s)", flush=True)
    print(f"Banco: {BANCO}", flush=True)
    print("=" * 65 + "\n", flush=True)

    while True:
        tentativa = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        print(f"[{tentativa}] Tentando consultar a API...", flush=True)

        try:
            dados = consultar_api()
            novas = salvar_leituras(dados)
            horario = time.strftime("%H:%M:%S")

            atualizar_estado(
                ok=True,
                ultimo_sucesso=horario,
                ultima_tentativa=tentativa,
                erro=None,
                recebidas=len(dados),
                novas=novas,
                coletas=obter_estado()["coletas"] + 1
            )

            if dados:
                print(
                    f"[SUCESSO {horario}] API respondeu; "
                    f"{len(dados)} leitura(s) recebida(s).",
                    flush=True
                )
                print(
                    f"[BANCO] {novas} registro(s) novo(s) gravado(s) em dados.db.",
                    flush=True
                )
                print(
                    "[CAMPOS] " + ", ".join(dados[0].keys()),
                    flush=True
                )
            else:
                print(
                    f"[ATENÇÃO {horario}] API respondeu, mas retornou zero leituras.",
                    flush=True
                )

            print(
                f"[STATUS] Coleta nº {obter_estado()['coletas']} concluída. "
                f"Total recebido nesta tentativa: {len(dados)}.",
                flush=True
            )

        except urllib.error.HTTPError as erro:
            mensagem = f"HTTP {erro.code}: {erro.reason}"
            atualizar_estado(
                ok=False,
                ultima_tentativa=tentativa,
                erro=mensagem
            )
            print(f"[ERRO HTTP] {mensagem}", flush=True)

        except urllib.error.URLError as erro:
            mensagem = f"Falha de conexão: {erro.reason}"
            atualizar_estado(
                ok=False,
                ultima_tentativa=tentativa,
                erro=mensagem
            )
            print(f"[ERRO DE CONEXÃO] {mensagem}", flush=True)

        except Exception as erro:
            mensagem = f"{type(erro).__name__}: {erro}"
            atualizar_estado(
                ok=False,
                ultima_tentativa=tentativa,
                erro=mensagem
            )
            print(f"[ERRO NA COLETA] {mensagem}", flush=True)

        print(
            f"[AGUARDANDO] Próxima tentativa em {args.intervalo} segundo(s).\n",
            flush=True
        )
        time.sleep(args.intervalo)


# ============================================================
# 6. DADOS QUE O DASHBOARD 2 CONSOME
# ============================================================

def montar_estado():
    con = conectar()
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM leituras"
        ).fetchone()[0]

        nomes = con.execute("""
            SELECT DISTINCT maquina
            FROM leituras
            ORDER BY maquina
        """).fetchall()

        maquinas = []
        serie = {}

        for registro in nomes:
            nome_original = registro["maquina"]

            ultimo = con.execute("""
                SELECT id, timestamp, maquina, temperatura,
                       vibracao, pressao, producao, status
                FROM leituras
                WHERE maquina IS ?
                ORDER BY rowid DESC
                LIMIT 1
            """, (nome_original,)).fetchone()

            if ultimo is None:
                continue

            qtd = con.execute(
                "SELECT COUNT(*) FROM leituras WHERE maquina IS ?",
                (nome_original,)
            ).fetchone()[0]

            nome = nome_original or "(sem nome)"
            maquina = dict(ultimo)
            maquina["maquina"] = nome
            maquina["qtd"] = qtd
            maquinas.append(maquina)

            pontos = con.execute("""
                SELECT timestamp, temperatura
                FROM leituras
                WHERE maquina IS ?
                ORDER BY rowid DESC
                LIMIT 60
            """, (nome_original,)).fetchall()

            serie[nome] = [
                [ponto["timestamp"], ponto["temperatura"]]
                for ponto in reversed(pontos)
            ]

        recentes = con.execute("""
            SELECT id, timestamp, maquina, temperatura,
                   vibracao, pressao, producao, status
            FROM leituras
            ORDER BY rowid DESC
            LIMIT 25
        """).fetchall()

        return {
            "coletor": obter_estado(),
            "base": args.base,
            "total": total,
            "maquinas": maquinas,
            "serie": serie,
            "recentes": [dict(item) for item in recentes]
        }
    finally:
        con.close()


# ============================================================
# 6b. HISTÓRICO COMPLETO PARA ANÁLISE (todas as variáveis do raw)
# ============================================================

def montar_historico(limite=1500, base=100):
    """Por máquina: as primeiras `base` leituras (linha de base) e as últimas
    `limite` leituras, com todos os campos numéricos do JSON original."""
    con = conectar()
    try:
        nomes = [r["maquina"] for r in con.execute(
            "SELECT DISTINCT maquina FROM leituras ORDER BY maquina"
        ).fetchall()]

        saida = []
        for nome in nomes:
            ini = con.execute(
                "SELECT rowid AS rid, raw FROM leituras WHERE maquina IS ? "
                "ORDER BY rowid ASC LIMIT ?", (nome, base)
            ).fetchall()
            fim = con.execute(
                "SELECT rowid AS rid, raw FROM leituras WHERE maquina IS ? "
                "ORDER BY rowid DESC LIMIT ?", (nome, limite)
            ).fetchall()

            vistos, linhas, tipo = set(), [], ""
            for r in list(ini) + list(reversed(fim)):
                if r["rid"] in vistos:
                    continue
                vistos.add(r["rid"])
                try:
                    dados = json.loads(r["raw"] or "{}")
                except json.JSONDecodeError:
                    dados = {}
                tipo = dados.get("tipo", tipo)
                linha = {
                    "rowid": r["rid"],
                    "timestamp": dados.get("timestamp"),
                    "status": dados.get("status"),
                }
                for chave, valor in dados.items():
                    if chave in ("id", "timestamp", "maquina", "tipo", "status"):
                        continue
                    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                        linha[chave] = valor
                linhas.append(linha)

            saida.append({
                "maquina": nome or "(sem nome)",
                "tipo": tipo,
                "linhas": linhas,
            })
        return saida
    finally:
        con.close()


# ============================================================
# 7. EXPORTAÇÃO CSV
# ============================================================

def montar_csv():
    con = conectar()
    try:
        buffer = io.StringIO()
        escritor = csv.writer(buffer, delimiter=";")
        escritor.writerow(CAMPOS)

        registros = con.execute(
            f"SELECT {', '.join(CAMPOS)} FROM leituras ORDER BY rowid"
        )
        for registro in registros:
            escritor.writerow(list(registro))

        return buffer.getvalue().encode("utf-8-sig")
    finally:
        con.close()


# ============================================================
# 8. SERVIDOR HTTP EXCLUSIVO DO DASHBOARD 2
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def enviar(self, corpo, tipo="application/json; charset=utf-8", codigo=200, extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        for chave, valor in (extra or {}).items():
            self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        rota = self.path.split("?", 1)[0]
        try:
            if rota in ("/", "/index.html"):
                if not ARQUIVO_HTML.exists():
                    raise FileNotFoundError(
                        f"Não encontrei {ARQUIVO_HTML.name} na pasta do coletor."
                    )
                self.enviar(
                    ARQUIVO_HTML.read_bytes(),
                    "text/html; charset=utf-8"
                )

            elif rota == "/api/estado":
                corpo = json.dumps(
                    montar_estado(),
                    ensure_ascii=False
                ).encode("utf-8")
                self.enviar(corpo)

            elif rota == "/api/historico":
                query = parse_qs(urlparse(self.path).query)
                try:
                    limite = int(query.get("limite", ["1500"])[0])
                except ValueError:
                    limite = 1500
                limite = max(100, min(limite, 20000))
                corpo = json.dumps(
                    montar_historico(limite),
                    ensure_ascii=False
                ).encode("utf-8")
                self.enviar(corpo)

            elif rota == "/api/csv":
                self.enviar(
                    montar_csv(),
                    "text/csv; charset=utf-8",
                    extra={
                        "Content-Disposition":
                        "attachment; filename=leituras_dashboard2.csv"
                    }
                )

            elif rota == "/api/diagnostico":
                diagnostico = {
                    "servidor": "online",
                    "dashboard2_existe": ARQUIVO_HTML.exists(),
                    "api": URL_API,
                    "banco": str(BANCO),
                    "banco_existe": BANCO.exists(),
                    "coletor": obter_estado()
                }
                corpo = json.dumps(
                    diagnostico,
                    ensure_ascii=False,
                    indent=2
                ).encode("utf-8")
                self.enviar(corpo)

            else:
                self.enviar(
                    json.dumps({"erro": "Rota não encontrada"}).encode("utf-8"),
                    codigo=404
                )

        except Exception as erro:
            print(f"[ERRO SERVIDOR] {type(erro).__name__}: {erro}", flush=True)
            self.enviar(
                json.dumps({"erro": str(erro)}, ensure_ascii=False).encode("utf-8"),
                codigo=500
            )

    def log_message(self, formato, *args_log):
        # Mantém o terminal limpo; as mensagens de coleta continuam visíveis.
        pass


# ============================================================
# 9. INICIALIZAÇÃO
# ============================================================

def iniciar():
    criar_tabela()

    thread_coleta = threading.Thread(
        target=loop_coleta,
        name="coletor-dashboard2",
        daemon=True
    )
    thread_coleta.start()

    servidor = ThreadingHTTPServer(
        ("0.0.0.0", args.porta),
        Handler
    )

    print("\n" + "=" * 65, flush=True)
    print("DASHBOARD 2 - SERVIDOR INICIADO", flush=True)
    print(f"Painel:      http://localhost:{args.porta}", flush=True)
    print(f"Diagnóstico: http://localhost:{args.porta}/api/diagnostico", flush=True)
    print(f"API:         {URL_API}", flush=True)
    print(f"Banco:       {BANCO}", flush=True)
    print("O Dashboard normal não será alterado.", flush=True)
    print("Mantenha esta janela aberta. CTRL+C encerra.", flush=True)
    print("=" * 65 + "\n", flush=True)

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando o servidor do Dashboard 2...", flush=True)
    finally:
        servidor.server_close()


if __name__ == "__main__":
    iniciar()