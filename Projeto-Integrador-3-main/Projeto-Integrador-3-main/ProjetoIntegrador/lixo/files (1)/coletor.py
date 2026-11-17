"""
Coletor + servidor do painel (só biblioteca padrão do Python, nada para instalar).

Uso:
    python coletor.py
    python coletor.py --base http://10.128.168.92:8080 --rota /maquinas --intervalo 5 --porta 5000

Depois abra no navegador:  http://localhost:5000
"""
import argparse
import csv
import io
import json
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://10.128.168.92:8080", help="URL da API (com a porta)")
ap.add_argument("--rota", default="/maquinas", help="rota que devolve as leituras")
ap.add_argument("--intervalo", type=float, default=5, help="segundos entre coletas")
ap.add_argument("--timeout", type=float, default=15, help="segundos de espera por resposta da API")
ap.add_argument("--porta", type=int, default=5000, help="porta do painel local")
ap.add_argument("--db", default="dados.db", help="arquivo do banco SQLite")
args = ap.parse_args()

# abre a API ignorando o proxy do Windows (proxy costuma causar "timed out" em IPs internos)
ABRIR = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

AQUI = Path(__file__).parent
CAMPOS = ["id", "timestamp", "maquina", "temperatura", "vibracao", "pressao", "producao", "status"]
vistos = {"campos": False}
estado = {"ok": False, "ultimo_sucesso": None, "erro": "aguardando a primeira coleta",
          "coletas": 0, "recebidas": 0, "novas": 0}


def conectar():
    # timeout: espera o banco liberar em vez de dar "database is locked"
    con = sqlite3.connect(args.db, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def criar_tabela():
    con = conectar()
    # WAL permite o painel LER enquanto o coletor GRAVA, sem travar um ao outro
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER, timestamp TEXT, maquina TEXT,
            temperatura REAL, vibracao REAL, pressao REAL, producao REAL,
            status TEXT, raw TEXT,
            coletado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (raw))"""
    )
    con.commit()
    con.close()


def loop_coleta():
    con = conectar()
    while True:
        hora = time.strftime("%H:%M:%S")
        try:
            with ABRIR(args.base + args.rota, timeout=args.timeout) as r:
                dados = json.loads(r.read().decode("utf-8"))
            if isinstance(dados, dict):
                listas = [v for v in dados.values() if isinstance(v, list)]
                dados = listas[0] if listas else [dados]
            dados = [d for d in dados if isinstance(d, dict)]
            if dados and not vistos["campos"]:
                vistos["campos"] = True
                print("Campos recebidos da API:", list(dados[0].keys()))
                print("Exemplo:", json.dumps(dados[0], ensure_ascii=False))

            # grava em ordem cronológica: o painel considera "última leitura" a de maior rowid,
            # então se a API devolver a lista do mais novo para o mais antigo, ficaria invertido
            dados.sort(key=lambda d: str(d.get("timestamp") or ""))

            novos = 0
            for d in dados:
                cur = con.execute(
                    """INSERT OR IGNORE INTO leituras
                       (id, timestamp, maquina, temperatura, vibracao, pressao, producao, status, raw)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [d.get(c) for c in CAMPOS] + [json.dumps(d)],
                )
                novos += cur.rowcount
            con.commit()
            estado.update(ok=True, ultimo_sucesso=hora, erro=None,
                          recebidas=len(dados), novas=novos)
            estado["coletas"] += 1
            # DIAGNÓSTICO: se "novas" ficar sempre 0, a própria API está devolvendo dados repetidos
            print(f"[{hora}] recebidas={len(dados)} novas={novos}")
        except Exception as e:
            estado.update(ok=False, erro=str(e))
            print(f"[{hora}] ERRO na coleta: {e}")   # antes o erro era engolido em silêncio
        time.sleep(args.intervalo)


def montar_estado():
    con = conectar()
    cols = ", ".join(CAMPOS)
    total = con.execute("SELECT COUNT(*) FROM leituras").fetchone()[0]
    nomes = [r[0] for r in con.execute("SELECT DISTINCT maquina FROM leituras ORDER BY maquina")]
    maquinas, serie = [], {}
    for m in nomes:
        ult = con.execute(
            f"SELECT {cols}, raw FROM leituras WHERE maquina IS ? ORDER BY rowid DESC LIMIT 1", (m,)
        ).fetchone()
        if ult is None:
            continue
        qtd = con.execute("SELECT COUNT(*) FROM leituras WHERE maquina IS ?", (m,)).fetchone()[0]
        nome = m or "(sem nome)"
        maquinas.append({**dict(ult), "maquina": nome, "qtd": qtd})
        pts = con.execute(
            "SELECT timestamp, temperatura FROM leituras WHERE maquina IS ? ORDER BY rowid DESC LIMIT 60", (m,)
        ).fetchall()
        serie[nome] = [[p["timestamp"], p["temperatura"]] for p in reversed(pts)]
    recentes = [dict(r) for r in con.execute(
        f"SELECT {cols} FROM leituras ORDER BY rowid DESC LIMIT 25")]
    con.close()
    return {"coletor": dict(estado), "base": args.base, "total": total,
            "maquinas": maquinas, "serie": serie, "recentes": recentes}


def montar_csv():
    con = conectar()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CAMPOS)
    for r in con.execute(f"SELECT {', '.join(CAMPOS)} FROM leituras ORDER BY rowid"):
        w.writerow(list(r))
    con.close()
    return buf.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def _enviar(self, corpo, tipo, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        rota = self.path.split("?")[0]
        try:
            if rota in ("/", "/index.html"):
                self._enviar((AQUI / "dashboard2.html").read_bytes(), "text/html; charset=utf-8")
            elif rota == "/api/estado":
                self._enviar(json.dumps(montar_estado()).encode("utf-8"), "application/json")
            elif rota == "/api/csv":
                self._enviar(montar_csv(), "text/csv; charset=utf-8",
                             {"Content-Disposition": "attachment; filename=leituras.csv"})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # navegador fechou a aba no meio da resposta
        except Exception as e:
            print("ERRO ao responder", rota, "->", e)
            self.send_error(500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    criar_tabela()
    threading.Thread(target=loop_coleta, daemon=True).start()
    print(f"Coletando de {args.base}{args.rota} a cada {args.intervalo}s -> {args.db}")
    print(f"Painel: http://localhost:{args.porta}   (Ctrl+C para parar)")
    ThreadingHTTPServer(("0.0.0.0", args.porta), Handler).serve_forever()