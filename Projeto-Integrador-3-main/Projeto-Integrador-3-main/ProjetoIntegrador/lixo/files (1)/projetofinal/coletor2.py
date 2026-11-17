"""
Coletor + servidor do painel (só biblioteca padrão do Python, nada para instalar).

Uso:
    python coletor2.py
    python coletor2.py --base http://10.128.168.92:8080 --rota /maquinas --intervalo 5 --porta 5000

Depois abra no navegador:  http://localhost:5000
"""
import argparse
import base64
import hmac
import math
import secrets
import statistics
import unicodedata
from datetime import datetime
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
ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 para liberar o painel na rede")
ap.add_argument("--usuario", default="admin", help="usuario do painel")
ap.add_argument("--senha", default=None, help="senha do painel (se omitida, e gerada e mostrada ao iniciar)")
ap.add_argument("--db", default="dados.db", help="arquivo do banco SQLite")
args = ap.parse_args()
SENHA = args.senha or secrets.token_urlsafe(6)

ABRIR = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

AQUI = Path(__file__).parent
CAMPOS = ["id", "timestamp", "maquina", "temperatura", "vibracao", "pressao", "producao", "status"]
vistos = {"campos": False}
estado = {"ok": False, "ultimo_sucesso": None, "erro": "aguardando a primeira coleta",
          "coletas": 0, "recebidas": 0, "novas": 0}


def conectar():
    con = sqlite3.connect(args.db, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def criar_tabela():
    con = conectar()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER, timestamp TEXT, maquina TEXT,
            temperatura REAL, vibracao REAL, pressao REAL, producao REAL,
            status TEXT, raw TEXT,
            coletado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (raw))"""
    )
    con.execute("""CREATE TABLE IF NOT EXISTS quarentena (
            raw TEXT UNIQUE, motivo TEXT, coletado_em TEXT DEFAULT CURRENT_TIMESTAMP)""")
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

            dados.sort(key=lambda d: str(d.get("timestamp") or ""))

            novos = 0
            for d in dados:
                motivo = validar(d)
                if motivo:
                    con.execute("INSERT OR IGNORE INTO quarentena (raw, motivo) VALUES (?,?)",
                                (json.dumps(d), motivo))
                    continue
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
            print(f"[{hora}] recebidas={len(dados)} novas={novos}")
        except Exception as e:
            estado.update(ok=False, erro=str(e))
            print(f"[{hora}] ERRO na coleta: {e}")
        time.sleep(args.intervalo)


def calcular_boxplot(valores):
    if not valores:
        return {"min": 0, "q1": 0, "mediana": 0, "q3": 0, "max": 0}
    vals = sorted([v for v in valores if v is not None])
    if not vals:
        return {"min": 0, "q1": 0, "mediana": 0, "q3": 0, "max": 0}
    n = len(vals)
    def percentil(p):
        k = (n - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return vals[int(k)]
        return vals[int(f)] * (c - k) + vals[int(c)] * (k - f)
    return {
        "min": round(vals[0], 2),
        "q1": round(percentil(0.25), 2),
        "mediana": round(percentil(0.50), 2),
        "q3": round(percentil(0.75), 2),
        "max": round(vals[-1], 2)
    }


def classificar_por_criterios(r):
    temp = fl(r.get("temperatura"))
    vib = fl(r.get("vibracao"))
    st = norm(r.get("status"))
    if "CRITIC" in st or "FALHA" in st or "PERIGO" in st:
        return "critico"
    if "ATENC" in st or "NOK" in st or "ALERTA" in st or "MANUT" in st:
        return "nok"
    if "NORMAL" in st or "OPER" in st or "OK" in st:
        return "ok"

    if temp is not None and temp > 67:
        return "critico"
    if vib is not None and vib > 3.6:
        return "critico"
        
    if temp is not None and (62 < temp <= 67):
        return "nok"
    if vib is not None and (2.8 < vib <= 3.6):
        return "nok"
        
    return "ok"


def montar_estado():
    con = conectar()
    cols = ", ".join(CAMPOS)
    total = con.execute("SELECT COUNT(*) FROM leituras").fetchone()[0]
    nomes = [r[0] for r in con.execute("SELECT DISTINCT maquina FROM leituras ORDER BY maquina")]
    maquinas, serie = [], {}
    
    temps_ok, temps_nok, temps_critico = [], [], []
    transicoes = {}

    for m in nomes:
        ult_row = con.execute(
            f"SELECT {cols}, raw FROM leituras WHERE maquina IS ? ORDER BY rowid DESC LIMIT 1", (m,)
        ).fetchone()
        if ult_row is None:
            continue
        
        # Converte o sqlite3.Row para dicionário padrão do Python para evitar erros com .get()
        ult = dict(ult_row)
        
        qtd = con.execute("SELECT COUNT(*) FROM leituras WHERE maquina IS ?", (m,)).fetchone()[0]
        nome = m or "(sem nome)"
        maquinas.append({**ult, "maquina": nome, "qtd": qtd})
        
        pts = con.execute(
            "SELECT timestamp, temperatura FROM leituras WHERE maquina IS ? ORDER BY rowid DESC LIMIT 60", (m,)
        ).fetchall()
        serie[nome] = [[p["timestamp"], p["temperatura"]] for p in reversed(pts)]

    # Coletar dados globais aplicando os critérios exatos para o Boxplot
    todas_leituras = con.execute(f"SELECT maquina, temperatura, vibracao, status FROM leituras").fetchall()
    for r_row in todas_leituras:
        r = dict(r_row) # Conversão segura para dict
        temp = fl(r.get("temperatura"))
        if temp is not None:
            cat = classificar_por_criterios(r)
            if cat == "ok":
                temps_ok.append(temp)
            elif cat == "nok":
                temps_nok.append(temp)
            else:
                temps_critico.append(temp)

    # Coletar transições de estados
    seq_trans = con.execute("SELECT maquina, status FROM leituras ORDER BY rowid").fetchall()
    for i in range(len(seq_trans) - 1):
        if seq_trans[i]["maquina"] == seq_trans[i+1]["maquina"]:
            de = seq_trans[i]["status"] or "NORMAL"
            para = seq_trans[i+1]["status"] or "NORMAL"
            if de != para:
                chave = f"{de} → {para}"
                transicoes[chave] = transicoes.get(chave, 0) + 1

    boxplot_dados = {
        "ok": calcular_boxplot(temps_ok),
        "nok": calcular_boxplot(temps_nok),
        "critico": calcular_boxplot(temps_critico)
    }

    recentes_rows = con.execute(f"SELECT {cols} FROM leituras ORDER BY rowid DESC LIMIT 25").fetchall()
    recentes = [dict(r) for r in recentes_rows] # Conversão segura aqui também
    
    con.close()
    return {
        "coletor": dict(estado), "base": args.base, "total": total,
        "maquinas": maquinas, "serie": serie, "recentes": recentes,
        "boxplot": boxplot_dados, "transicoes": transicoes
    }

def montar_csv():
    con = conectar()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CAMPOS)
    for r in con.execute(f"SELECT {', '.join(CAMPOS)} FROM leituras ORDER BY rowid"):
        w.writerow(list(r))
    con.close()
    return buf.getvalue().encode("utf-8-sig")


NUM = ["temperatura", "vibracao", "pressao", "producao"]


def validar(d):
    if not isinstance(d.get("maquina"), str) or not d["maquina"].strip():
        return "maquina ausente"
    if not d.get("timestamp"):
        return "timestamp ausente"
    for k in NUM:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return k + " com tipo invalido"
        if v < 0 and k != "temperatura":
            return k + " negativo"
    return None


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "")) if not unicodedata.combining(c)).upper().strip()


def fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def analisar():
    con = conectar()
    linhas = con.execute("SELECT timestamp, maquina, temperatura, vibracao, pressao, producao, status "
                         "FROM leituras ORDER BY rowid").fetchall()
    qtot = con.execute("SELECT COUNT(*) FROM quarentena").fetchone()[0]
    con.close()
    por = {}
    for r in linhas:
        t = ts(r["timestamp"])
        if t is not None and r["maquina"]:
            por.setdefault(r["maquina"], []).append((t, r))
    maqs, anom = [], []
    for nome, L in por.items():
        L.sort(key=lambda x: x[0])
        maqs.append({"maquina": nome, "leituras": len(L)})
    return {"maquinas": maqs, "anomalias": anom[:20]}


def autorizado(h):
    try:
        u, _, sn = base64.b64decode(h.headers.get("Authorization", "").split(" ", 1)[1]).decode().partition(":")
        return hmac.compare_digest(u.encode(), args.usuario.encode()) and hmac.compare_digest(sn.encode(), SENHA.encode())
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def _enviar(self, corpo, tipo, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        rota = self.path.split("?")[0]
        if not autorizado(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Painel industrial"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if rota in ("/", "/index.html"):
                # APONTANDO PARA O NOVO DASHBOARD 3
                self._enviar((AQUI / "dashboard3.html").read_bytes(), "text/html; charset=utf-8")
            elif rota == "/api/estado":
                self._enviar(json.dumps(montar_estado()).encode("utf-8"), "application/json")
            elif rota == "/api/analise":
                self._enviar(json.dumps(analisar()).encode("utf-8"), "application/json")
            elif rota == "/api/csv":
                self._enviar(montar_csv(), "text/csv; charset=utf-8",
                             {"Content-Disposition": "attachment; filename=leituras.csv"})
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
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
    print(f"Login:  usuario={args.usuario}  senha={SENHA}")
    ThreadingHTTPServer((args.host, args.porta), Handler).serve_forever()