"""
Coletor + servidor do painel (só biblioteca padrão do Python, nada para instalar).

Uso:
    python coletor.py
    python coletor.py --base http://localhost:8080 --rota /api/maquinas --intervalo 5 --porta 5000

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
ap.add_argument("--base", default="http://localhost:8080", help="URL da API (com a porta)")
ap.add_argument("--rota", default="/api/maquinas", help="rota que devolve as leituras")
ap.add_argument("--intervalo", type=float, default=5, help="segundos entre coletas")
ap.add_argument("--porta", type=int, default=5000, help="porta do painel local")
ap.add_argument("--db", default="leituras.db", help="arquivo do banco SQLite")
args = ap.parse_args()

AQUI = Path(__file__).parent
CAMPOS = ["id", "timestamp", "maquina", "temperatura", "vibracao", "pressao", "producao", "status"]
estado = {"ok": False, "ultimo_sucesso": None, "erro": "aguardando a primeira coleta", "coletas": 0}


def conectar():
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    return con


def criar_tabela():
    con = conectar()
    con.execute(
        """CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER, timestamp TEXT, maquina TEXT,
            temperatura REAL, vibracao REAL, pressao REAL, producao REAL,
            status TEXT, raw TEXT,
            coletado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (timestamp, maquina))"""
    )
    con.commit()
    con.close()


def loop_coleta():
    con = conectar()
    while True:
        try:
            with urllib.request.urlopen(args.base + args.rota, timeout=5) as r:
                dados = json.loads(r.read().decode("utf-8"))
            if isinstance(dados, dict):
                dados = [dados]
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
            estado.update(ok=True, ultimo_sucesso=time.strftime("%H:%M:%S"), erro=None,
                          recebidas=len(dados), novas=novos)
            estado["coletas"] += 1
        except Exception as e:
            estado.update(ok=False, erro=str(e))
        time.sleep(args.intervalo)


def montar_estado():
    con = conectar()
    cols = ", ".join(CAMPOS)
    total = con.execute("SELECT COUNT(*) FROM leituras").fetchone()[0]
    nomes = [r[0] for r in con.execute("SELECT DISTINCT maquina FROM leituras ORDER BY maquina")]
    maquinas, serie = [], {}
    for m in nomes:
        ult = con.execute(
            f"SELECT {cols} FROM leituras WHERE maquina=? ORDER BY timestamp DESC LIMIT 1", (m,)
        ).fetchone()
        qtd = con.execute("SELECT COUNT(*) FROM leituras WHERE maquina=?", (m,)).fetchone()[0]
        maquinas.append({**dict(ult), "qtd": qtd})
        pts = con.execute(
            "SELECT timestamp, temperatura FROM leituras WHERE maquina=? ORDER BY timestamp DESC LIMIT 60", (m,)
        ).fetchall()
        serie[m] = [[p["timestamp"], p["temperatura"]] for p in reversed(pts)]
    recentes = [dict(r) for r in con.execute(
        f"SELECT {cols} FROM leituras ORDER BY timestamp DESC LIMIT 25")]
    con.close()
    return {"coletor": estado, "base": args.base, "total": total,
            "maquinas": maquinas, "serie": serie, "recentes": recentes}


def montar_csv():
    con = conectar()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CAMPOS)
    for r in con.execute(f"SELECT {', '.join(CAMPOS)} FROM leituras ORDER BY timestamp"):
        w.writerow(list(r))
    con.close()
    return buf.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def _enviar(self, corpo, tipo, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        rota = self.path.split("?")[0]
        if rota in ("/", "/index.html"):
            self._enviar((AQUI / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif rota == "/api/estado":
            self._enviar(json.dumps(montar_estado()).encode("utf-8"), "application/json")
        elif rota == "/api/csv":
            self._enviar(montar_csv(), "text/csv; charset=utf-8",
                         {"Content-Disposition": "attachment; filename=leituras.csv"})
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    criar_tabela()
    threading.Thread(target=loop_coleta, daemon=True).start()
    print(f"Coletando de {args.base} a cada {args.intervalo}s -> {args.db}")
    print(f"Painel: http://localhost:{args.porta}   (Ctrl+C para parar)")
    ThreadingHTTPServer(("0.0.0.0", args.porta), Handler).serve_forever()