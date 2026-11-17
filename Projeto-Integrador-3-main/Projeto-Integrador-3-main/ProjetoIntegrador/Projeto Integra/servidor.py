"""Mini servidor local: entrega a página e repassa as chamadas para a API (evita CORS).
Uso:  python servidor.py   -> abra http://localhost:8000
"""
import os, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

API = "http://10.107.67.92:8080"      # endereço da API da ESP
PORTA = 8000
PAGINA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analise-exploratoria.html")

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, tipo):
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGINA, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        try:
            with urllib.request.urlopen(API + self.path, timeout=8) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read(), "text/plain")
        except Exception as e:
            self._send(502, f"Falha ao acessar {API}: {e}".encode(), "text/plain; charset=utf-8")

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"Pronto! Abra http://localhost:{PORTA}  (repassando para {API})")
    ThreadingHTTPServer(("0.0.0.0", PORTA), H).serve_forever()
