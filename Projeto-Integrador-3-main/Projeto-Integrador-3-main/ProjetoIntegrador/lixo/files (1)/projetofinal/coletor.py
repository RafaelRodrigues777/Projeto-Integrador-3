"""
Coletor + servidor do painel (só biblioteca padrão do Python, nada para instalar).

Uso:
    python coletor.py
    python coletor.py --base http://10.128.168.92:8080 --rota /maquinas --intervalo 5 --porta 5000

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
                print("Exemplo:", json.dumps(dados[0], ensure_ascii=False))

            # grava em ordem cronológica: o painel considera "última leitura" a de maior rowid,
            # então se a API devolver a lista do mais novo para o mais antigo, ficaria invertido
            dados.sort(key=lambda d: str(d.get("timestamp") or ""))

            novos = 0
            for d in dados:
                motivo = validar(d)
                if motivo:   # registro inconsistente vai para quarentena, nao para a analise
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


# ---------------- validacao ----------------
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


# ---------------- analises (media/desvio, Pearson, regressao linear) ----------------
# ORDEM DO PROCESSO ASSUMIDA: ajuste conforme o fluxo real da fabrica simulada
FLUXO = ["TORNO_CNC_01", "PRENSA_HIDRAULICA_01", "ESTEIRA_01",
         "ROBO_SOLDAGEM_01", "COMPRESSOR_01", "EMBALADORA_01"]


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


def pearson(x, y):
    n = len(x)
    if n < 8:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx, sy = sum((a - mx) ** 2 for a in x), sum((b - my) ** 2 for b in y)
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx * sy)


def inclinacao_min(pts):
    """regressao linear: variacao por minuto nas ultimas 120 leituras"""
    pts = pts[-120:]
    if len(pts) < 8:
        return 0.0
    x = [(t - pts[0][0]) / 60 for t, _ in pts]
    y = [v for _, v in pts]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sum((a - mx) ** 2 for a in x)
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / sx if sx else 0.0


def analisar():
    con = conectar()
    linhas = con.execute("SELECT timestamp, maquina, temperatura, vibracao, pressao, producao, status "
                         "FROM leituras ORDER BY rowid").fetchall()
    qtot = con.execute("SELECT COUNT(*) FROM quarentena").fetchone()[0]
    qmot = [{"motivo": r[0], "n": r[1]} for r in con.execute(
        "SELECT motivo, COUNT(*) FROM quarentena GROUP BY motivo")]
    con.close()
    por = {}
    for r in linhas:
        t = ts(r["timestamp"])
        if t is not None and r["maquina"]:
            por.setdefault(r["maquina"], []).append((t, r))
    maqs, anom, corr, recs = [], [], [], []
    for nome, L in por.items():
        L.sort(key=lambda x: x[0])
        fim = L[-1][0]
        op = parado = obs = inc = 0.0
        paradas = p_rec = 0
        for (t0, a), (t1, b) in zip(L, L[1:]):
            dt, sa, sb = t1 - t0, norm(a["status"]), norm(b["status"])
            pa, pb = fl(a["producao"]), fl(b["producao"])
            if pa is not None and pb is not None:
                inc += pb - pa if pb >= pa else pb          # queda = reinicio do contador
            if 0 < dt <= 60:                                 # lacunas > 1 min nao contam
                obs += dt
                op += dt if sa == "OPERANDO" else 0
                parado += dt if "PARAD" in sa else 0
            if "PARAD" in sb and "PARAD" not in sa:
                paradas += 1
                p_rec += 1 if t1 >= fim - 3600 else 0
        n_an = 0
        for var in ("temperatura", "vibracao"):
            v = [(t, fl(r[var])) for t, r in L if fl(r[var]) is not None]
            if len(v) < 30:
                continue
            m, sd = statistics.mean(x for _, x in v), statistics.pstdev(x for _, x in v)
            for t, x in (v[-100:] if sd else []):
                z = (x - m) / sd
                if abs(z) > 3:
                    n_an += 1
                    anom.append({"maquina": nome, "variavel": var, "valor": round(x, 2), "z": round(z, 1),
                                 "quando": datetime.fromtimestamp(t).strftime("%d/%m %H:%M:%S"), "_t": t})
        for a_, b_ in (("temperatura", "vibracao"), ("temperatura", "pressao")):
            par = [(fl(r[a_]), fl(r[b_])) for _, r in L if fl(r[a_]) is not None and fl(r[b_]) is not None]
            rr = pearson([p[0] for p in par], [p[1] for p in par])
            if rr is not None:
                corr.append({"maquina": nome, "par": a_ + " x " + b_, "r": round(rr, 2), "n": len(par),
                             "forca": "forte" if abs(rr) >= .7 else "moderada" if abs(rr) >= .4 else "fraca"})
        tend = inclinacao_min([(t, fl(r["temperatura"])) for t, r in L if fl(r["temperatura"]) is not None])
        ult = norm(L[-1][1]["status"])
        # RISCO: heuristica simples e explicavel (documentada no DOCUMENTACAO.md)
        risco = min(100, 10 * n_an + 15 * p_rec + (20 if ("MANUT" in ult or "PARAD" in ult) else 0)
                    + (15 if tend > 0.05 else 0))
        nivel = "alto" if risco >= 60 else "medio" if risco >= 30 else "baixo"
        disp = round(op / obs * 100, 1) if obs else None
        taxa = inc / op if op else 0
        jus = FLUXO[FLUXO.index(nome) + 1:] if (nome in FLUXO and paradas) else []
        maqs.append({"maquina": nome, "leituras": len(L), "producao": round(inc), "disponibilidade": disp,
                     "paradas": paradas, "paradas_1h": p_rec, "tempo_parado_s": round(parado),
                     "perdida": round(taxa * parado), "impactadas": jus, "anomalias": n_an,
                     "tendencia_temp": round(tend, 3), "risco": risco, "nivel": nivel})
        if nivel == "alto":
            recs.append(f"{nome}: risco ALTO ({risco}/100). Priorizar inspecao.")
        if disp is not None and disp < 80:
            recs.append(f"{nome}: disponibilidade de {disp}% (< 80%). Investigar a causa das paradas.")
        if p_rec >= 2:
            recs.append(f"{nome}: {p_rec} paradas na ultima hora (falha recorrente). Avaliar manutencao corretiva.")
        if tend > 0.05:
            recs.append(f"{nome}: temperatura subindo {tend:.2f} C/min. Verificar refrigeracao/lubrificacao.")
        if jus:
            recs.append(f"{nome}: paradas podem afetar a jusante: {', '.join(jus)}. Revisar estoque intermediario.")
    anom.sort(key=lambda a: -a["_t"])
    for a in anom:
        a.pop("_t")
    return {"maquinas": sorted(maqs, key=lambda m: -m["risco"]), "anomalias": anom[:20], "correlacoes": corr,
            "recomendacoes": recs, "fluxo": FLUXO, "quarentena": {"total": qtot, "motivos": qmot}}


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
            print("acesso negado de", self.client_address[0], "->", rota)
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Painel industrial"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if rota in ("/", "/index.html"):
                self._enviar((AQUI / "dashboard2.html").read_bytes(), "text/html; charset=utf-8")
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
    print(f"Login:  usuario={args.usuario}  senha={SENHA}")
    ThreadingHTTPServer((args.host, args.porta), Handler).serve_forever()