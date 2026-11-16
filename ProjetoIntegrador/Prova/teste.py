import sqlite3
# Inicialização de um banco local em memória para o laboratório
def setup_banco():
conn = sqlite3.connect(':memory:')
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE usuarios (
id INTEGER PRIMARY KEY,
username TEXT,
senha TEXT,
papel TEXT,
ip_origem_permitido TEXT
)
''')
# Usuários do sistema industrial
usuarios = [
(1, 'alice_op', '1234', 'Operador', '192.168.20.10'),
# VLAN Fábrica
(2, 'bob_eng', 'eng@2026', 'Engenheiro', '192.168.20.50'),
# VLAN Fábrica
(3, 'charlie_adm', 'admin!@#', 'Administrador',
'192.168.10.5'), # VLAN Admin
(4, 'estagiario_fin', 'estagio', 'Financeiro',
'192.168.10.99') # VLAN Admin
]
cursor.executemany('INSERT INTO usuarios VALUES (?, ?, ?, ?,
?)', usuarios)
conn.commit()
return conn
# --- SISTEMA SUPERVISÓRIO INDUSTRIAL (CÓDIGO LEGADO) ---
def autenticar(conn, username, senha):
cursor = conn.cursor()
query = f"SELECT id, username, papel FROM usuarios WHERE
username = '{username}' AND senha = '{senha}'"
cursor.execute(query)
user = cursor.fetchone()
if user:
return {"id": user[0], "username": user[1], "papel":
user[2]}
return None
def visualizar_dashboard(usuario_logado):
print(f"[DASHBOARD] Telemetria da Caldeira: 185°C | Pressão: 12
Bar (Acessado por {usuario_logado['username']})")
def reiniciar_servidor_plc(usuario_logado,
alvo_papel_requisicao=None):
papel_efetivo = alvo_papel_requisicao if alvo_papel_requisicao
else usuario_logado['papel']
if papel_efetivo != "Financeiro":
print(f"[CRÍTICO] Servidor PLC REINICIADO por
{usuario_logado['username']} (Papel detectado: {papel_efetivo})!")
else:
print("[BLOQUEADO] Financeiro não pode reiniciar o PLC.")
def gerenciar_usuarios(conn, usuario_logado, novo_user, novo_papel):
cursor = conn.cursor()
cursor.execute("INSERT INTO usuarios (username, senha, papel)
VALUES (?, 'padrao123', ?)", (novo_user, novo_papel))
conn.commit()
print(f"[ADMIN] Novo usuário '{novo_user}' ({novo_papel}) criado
com sucesso por {usuario_logado['username']}!")
# --- SIMULAÇÃO DE ATAQUES ---
if __name__ == "__main__":
db = setup_banco()
print("=== INICIANDO AUDITORIA DO SISTEMA INDUSTRIAL ===\n")
# Simulação 1
estagiario = autenticar(db, "estagiario_fin", "estagio")
print(f"1. Login realizado: {estagiario}")
reiniciar_servidor_plc(estagiario,
alvo_papel_requisicao="Engenheiro")
# Simulação 2
alice = autenticar(db, "alice_op", "1234")
print(f"\n2. Login realizado: {alice}")
gerenciar_usuarios(db, alice, "hacker_backdoor",
"Administrador")
# Simulação 3
invasor = autenticar(db, "charlie_adm' --", "qualquer_coisa")
print(f"\n3. Login via SQLi realizado: {invasor}"