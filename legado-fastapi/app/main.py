import base64
import calendar
import hashlib
import hmac
import os
import sqlite3
import time
from datetime import date

import bcrypt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

TURNOS = ("manha", "tarde")
TOKEN_TTL = 60 * 60 * 12

app = FastAPI(title="Caravanas do Templo", description="Ocupação do templo por dia e turno, a partir das caravanas cadastradas por seus líderes.")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def db():
    con = sqlite3.connect(env("DB_PATH", "caravanas.db"))
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS lideres(
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
            origem TEXT NOT NULL, usuario TEXT NOT NULL UNIQUE, senha_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS caravanas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lider_id INTEGER NOT NULL REFERENCES lideres(id),
            data TEXT NOT NULL, turno TEXT NOT NULL, pessoas INTEGER NOT NULL,
            transporte TEXT, obs TEXT);
        CREATE TABLE IF NOT EXISTS config(chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
        """
    )
    cols = {r["name"] for r in con.execute("PRAGMA table_info(caravanas)")}
    if "com_investidura" not in cols:
        con.execute("ALTER TABLE caravanas ADD COLUMN com_investidura INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE caravanas ADD COLUMN sem_investidura INTEGER NOT NULL DEFAULT 0")
    lcols = {r["name"] for r in con.execute("PRAGMA table_info(lideres)")}
    if "tipo" not in lcols:
        con.execute("ALTER TABLE lideres ADD COLUMN tipo TEXT NOT NULL DEFAULT 'estaca'")
    if "unidade" not in cols:
        con.execute("ALTER TABLE caravanas ADD COLUMN unidade TEXT")
        con.execute("ALTER TABLE caravanas ADD COLUMN tipo_unidade TEXT")
    con.execute("UPDATE caravanas SET unidade=(SELECT origem FROM lideres WHERE id=lider_id), tipo_unidade=(SELECT tipo FROM lideres WHERE id=lider_id) WHERE unidade IS NULL")
    # legado: caravanas sem a divisão com/sem investidura ficam como "sem" até serem editadas
    con.execute("UPDATE caravanas SET sem_investidura = pessoas WHERE com_investidura + sem_investidura = 0")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ---------- auth ----------
def _sign(payload: str) -> str:
    key = env("SECRET_KEY", "dev-only-secret").encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_token(role: str, uid: int = 0) -> str:
    payload = f"{role}:{uid}:{int(time.time()) + TOKEN_TTL}"
    return base64.urlsafe_b64encode(f"{payload}:{_sign(payload)}".encode()).decode()


def sessao(authorization: str = Header(default="")) -> dict:
    """Valida o token e devolve {role, id}. Qualquer usuário logado (admin ou líder)."""
    try:
        raw = base64.urlsafe_b64decode(authorization.removeprefix("Bearer ").encode()).decode()
        role, uid, exp, sig = raw.split(":")
        if not hmac.compare_digest(sig, _sign(f"{role}:{uid}:{exp}")) or int(exp) < time.time():
            raise ValueError
        return {"role": role, "id": int(uid)}
    except Exception:
        raise HTTPException(401, "Sessão inválida ou expirada")


def lider_atual(s=Depends(sessao), con=Depends(db)):
    if s["role"] != "lider":
        raise HTTPException(403, "Apenas líderes de caravana")
    row = con.execute("SELECT * FROM lideres WHERE id=?", (s["id"],)).fetchone()
    if not row:
        raise HTTPException(401, "Líder não encontrado")
    return row


def admin(s=Depends(sessao)):
    if s["role"] != "admin":
        raise HTTPException(403, "Acesso de administrador negado")


# ---------- schemas ----------
class Login(BaseModel):
    usuario: str
    senha: str


class CaravanaIn(BaseModel):
    data: date
    turno: str = Field(pattern="^(manha|tarde)$")
    com_investidura: int = Field(default=0, ge=0, le=1000)
    sem_investidura: int = Field(default=0, ge=0, le=1000)
    transporte: str | None = Field(default=None, max_length=40)
    obs: str | None = Field(default=None, max_length=300)
    tipo_unidade: str | None = Field(default=None, pattern="^(estaca|ala)$")
    unidade: str | None = Field(default=None, min_length=2, max_length=80)

    @model_validator(mode="after")
    def regras(self):
        if self.data.weekday() != 5:
            raise ValueError("Caravanas só podem ser cadastradas para sábados")
        if self.com_investidura + self.sem_investidura < 1:
            raise ValueError("Informe ao menos uma pessoa")
        return self

    @property
    def pessoas(self) -> int:
        return self.com_investidura + self.sem_investidura


class LiderIn(BaseModel):
    nome: str
    origem: str
    tipo: str = Field(default="estaca", pattern="^(estaca|ala)$")
    usuario: str
    senha: str = Field(min_length=6)


class CapacidadeIn(BaseModel):
    capacidade_turno: int = Field(gt=0)


def capacidade(con) -> int:
    r = con.execute("SELECT valor FROM config WHERE chave='capacidade_turno'").fetchone()
    return int(r["valor"]) if r else 224


def nivel(total: int, cap: int) -> str:
    if total == 0:
        return "vazio"
    pct = total / cap
    return "verde" if pct < 0.6 else "amarelo" if pct < 0.9 else "vermelho"


# ---------- rotas públicas ----------
@app.get("/api/calendario")
def calendario(mes: str, con=Depends(db), _=Depends(sessao)):
    """Ocupação por dia e turno no mês (`YYYY-MM`). Exige login. Mostra só origem e quantidade, sem dados pessoais."""
    try:
        ano, m = map(int, mes.split("-"))
        ultimo = calendar.monthrange(ano, m)[1]
    except Exception:
        raise HTTPException(422, "Use o formato YYYY-MM")
    cap = capacidade(con)
    rows = con.execute(
        """SELECT c.* FROM caravanas c
           JOIN lideres l ON l.id=c.lider_id WHERE c.data BETWEEN ? AND ? ORDER BY c.data""",
        (f"{mes}-01", f"{mes}-{ultimo:02d}"),
    ).fetchall()
    dias: dict = {}
    for r in rows:
        d = dias.setdefault(r["data"], {t: {"total": 0, "caravanas": [], "com": 0, "sem": 0} for t in TURNOS})
        d[r["turno"]]["total"] += r["pessoas"]
        d[r["turno"]]["com"] += r["com_investidura"]
        d[r["turno"]]["sem"] += r["sem_investidura"]
        d[r["turno"]]["caravanas"].append({"origem": r["unidade"], "tipo": r["tipo_unidade"], "pessoas": r["pessoas"], "com": r["com_investidura"], "sem": r["sem_investidura"], "transporte": r["transporte"]})
    for d in dias.values():
        for t in TURNOS:
            d[t]["nivel"] = nivel(d[t]["total"], cap)
            d[t]["vagas"] = max(0, cap - d[t]["total"])
    return {"mes": mes, "capacidade_turno": cap, "dias": dias}


@app.post("/api/login")
def login(body: Login, con=Depends(db)):
    """Login único. O usuário `admin` com a senha ADMIN_PASSWORD entra como administrador; os demais, como líderes."""
    if body.usuario == "admin":
        esperado = env("ADMIN_PASSWORD")
        if esperado and hmac.compare_digest(body.senha, esperado):
            return {"token": make_token("admin"), "role": "admin", "nome": "Administrador", "origem": ""}
        raise HTTPException(401, "Usuário ou senha incorretos")
    row = con.execute("SELECT * FROM lideres WHERE usuario=?", (body.usuario,)).fetchone()
    if not row or not bcrypt.checkpw(body.senha.encode(), row["senha_hash"].encode()):
        raise HTTPException(401, "Usuário ou senha incorretos")
    return {"token": make_token("lider", row["id"]), "role": "lider", "nome": row["nome"], "origem": row["origem"], "tipo": row["tipo"]}


# ---------- rotas do líder ----------
@app.get("/api/minhas-caravanas")
def minhas(l=Depends(lider_atual), con=Depends(db)):
    """Caravanas cadastradas pelo líder logado."""
    rows = con.execute("SELECT * FROM caravanas WHERE lider_id=? ORDER BY data", (l["id"],)).fetchall()
    return [dict(r) for r in rows]


def checar_vagas(con, body: CaravanaIn, ignorar: int = 0):
    """Impede ultrapassar a capacidade do turno naquele sábado."""
    cap = capacidade(con)
    usado = con.execute(
        "SELECT COALESCE(SUM(pessoas),0) FROM caravanas WHERE data=? AND turno=? AND id<>?",
        (body.data.isoformat(), body.turno, ignorar),
    ).fetchone()[0]
    if usado + body.pessoas > cap:
        vagas = max(0, cap - usado)
        raise HTTPException(409, f"Este turno só tem {vagas} vaga(s) disponível(is) (capacidade {cap}). Reduza o número de pessoas ou escolha outro turno/sábado.")


@app.post("/api/caravanas", status_code=201)
def criar(body: CaravanaIn, l=Depends(lider_atual), con=Depends(db)):
    """Cadastra uma caravana (somente aos sábados), limitada às vagas restantes do turno (409 se exceder)."""
    checar_vagas(con, body)
    cur = con.execute(
        "INSERT INTO caravanas(lider_id,data,turno,pessoas,transporte,obs,com_investidura,sem_investidura,unidade,tipo_unidade) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (l["id"], body.data.isoformat(), body.turno, body.pessoas, body.transporte, body.obs, body.com_investidura, body.sem_investidura,
         body.unidade or l["origem"], body.tipo_unidade or l["tipo"]),
    )
    return {"id": cur.lastrowid}


def _minha(con, cid: int, l):
    r = con.execute("SELECT * FROM caravanas WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Caravana não encontrada")
    if r["lider_id"] != l["id"]:
        raise HTTPException(403, "Esta caravana pertence a outro líder")


@app.put("/api/caravanas/{cid}")
def editar(cid: int, body: CaravanaIn, l=Depends(lider_atual), con=Depends(db)):
    """Edita uma caravana própria."""
    _minha(con, cid, l)
    checar_vagas(con, body, cid)
    con.execute(
        "UPDATE caravanas SET data=?,turno=?,pessoas=?,transporte=?,obs=?,com_investidura=?,sem_investidura=?,unidade=?,tipo_unidade=? WHERE id=?",
        (body.data.isoformat(), body.turno, body.pessoas, body.transporte, body.obs, body.com_investidura, body.sem_investidura,
         body.unidade or l["origem"], body.tipo_unidade or l["tipo"], cid),
    )
    return {"ok": True}


@app.delete("/api/caravanas/{cid}")
def apagar(cid: int, l=Depends(lider_atual), con=Depends(db)):
    """Remove uma caravana própria."""
    _minha(con, cid, l)
    con.execute("DELETE FROM caravanas WHERE id=?", (cid,))
    return {"ok": True}


# ---------- admin ----------
@app.post("/api/admin/lideres", status_code=201, dependencies=[Depends(admin)])
def criar_lider(body: LiderIn, con=Depends(db)):
    """Cria o acesso de um líder de caravana (somente admin)."""
    if body.usuario == "admin":
        raise HTTPException(409, "Usuário reservado")
    try:
        h = bcrypt.hashpw(body.senha.encode(), bcrypt.gensalt()).decode()
        cur = con.execute("INSERT INTO lideres(nome,origem,tipo,usuario,senha_hash) VALUES(?,?,?,?,?)", (body.nome, body.origem, body.tipo, body.usuario, h))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Usuário já existe")
    return {"id": cur.lastrowid}


@app.get("/api/admin/lideres", dependencies=[Depends(admin)])
def listar_lideres(con=Depends(db)):
    """Lista os líderes cadastrados (com a unidade padrão de cada um)."""
    return [dict(r) for r in con.execute("SELECT id,nome,origem,tipo,usuario FROM lideres ORDER BY origem")]


@app.get("/api/admin/unidades", dependencies=[Depends(admin)])
def unidades(con=Depends(db)):
    """Unidades (estacas e alas) com caravanas, pessoas e próximas idas (data, turno e pessoas)."""
    rows = con.execute(
        """SELECT tipo_unidade AS tipo, unidade, COUNT(*) AS caravanas, SUM(pessoas) AS pessoas
           FROM caravanas GROUP BY tipo_unidade, unidade"""
    ).fetchall()
    out = {(r["tipo"], r["unidade"]): {**dict(r), "proximas": []} for r in rows}
    for l in con.execute("SELECT tipo, origem FROM lideres"):
        out.setdefault((l["tipo"], l["origem"]), {"tipo": l["tipo"], "unidade": l["origem"], "caravanas": 0, "pessoas": 0, "proximas": []})
    for c in con.execute("SELECT tipo_unidade, unidade, data, turno, pessoas FROM caravanas WHERE data >= ? ORDER BY data, turno", (date.today().isoformat(),)):
        out[(c["tipo_unidade"], c["unidade"])]["proximas"].append({"data": c["data"], "turno": c["turno"], "pessoas": c["pessoas"]})
    return sorted(out.values(), key=lambda u: (u["tipo"], u["unidade"]))


def _periodo(de: str, ate: str, con):
    return con.execute(
        "SELECT c.*, l.nome AS lider FROM caravanas c JOIN lideres l ON l.id=c.lider_id WHERE c.data BETWEEN ? AND ? ORDER BY c.data, c.turno",
        (de, ate),
    ).fetchall()


@app.get("/api/admin/relatorio", dependencies=[Depends(admin)])
def relatorio(de: str, ate: str, con=Depends(db)):
    """Relatório do período (YYYY-MM-DD): totais, com/sem investidura, por unidade, por turno e linhas detalhadas."""
    rows = _periodo(de, ate, con)
    por_estaca: dict = {}
    por_turno = {t: {"pessoas": 0, "com": 0, "sem": 0} for t in TURNOS}
    for r in rows:
        e = por_estaca.setdefault(r["unidade"], {"origem": r["unidade"], "tipo": r["tipo_unidade"], "caravanas": 0, "pessoas": 0, "com": 0, "sem": 0})
        e["caravanas"] += 1
        for d in (e, por_turno[r["turno"]]):
            d["pessoas"] += r["pessoas"]
            d["com"] += r["com_investidura"]
            d["sem"] += r["sem_investidura"]
    return {
        "de": de, "ate": ate,
        "total_caravanas": len(rows), "total_pessoas": sum(r["pessoas"] for r in rows),
        "com_investidura": sum(r["com_investidura"] for r in rows),
        "sem_investidura": sum(r["sem_investidura"] for r in rows),
        "por_estaca": sorted(por_estaca.values(), key=lambda x: -x["pessoas"]),
        "por_turno": por_turno,
        "linhas": [
            {"data": r["data"], "turno": r["turno"], "origem": r["unidade"], "tipo": r["tipo_unidade"], "lider": r["lider"], "pessoas": r["pessoas"],
             "com": r["com_investidura"], "sem": r["sem_investidura"], "transporte": r["transporte"]}
            for r in rows
        ],
    }


@app.get("/api/admin/relatorio.csv", dependencies=[Depends(admin)])
def relatorio_csv(de: str, ate: str, con=Depends(db)):
    """Relatório detalhado do período em CSV."""
    import csv, io
    from fastapi.responses import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["data", "turno", "tipo", "unidade", "lider", "pessoas", "com_investidura", "sem_investidura", "transporte"])
    for r in _periodo(de, ate, con):
        w.writerow([r["data"], r["turno"], r["tipo_unidade"], r["unidade"], r["lider"], r["pessoas"], r["com_investidura"], r["sem_investidura"], r["transporte"] or ""])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="relatorio_{de}_{ate}.csv"'})


class SenhaIn(BaseModel):
    senha: str = Field(min_length=6)


@app.put("/api/admin/lideres/{lid}/senha", dependencies=[Depends(admin)])
def redefinir_senha(lid: int, body: SenhaIn, con=Depends(db)):
    """Redefine a senha de um líder."""
    h = bcrypt.hashpw(body.senha.encode(), bcrypt.gensalt()).decode()
    if con.execute("UPDATE lideres SET senha_hash=? WHERE id=?", (h, lid)).rowcount == 0:
        raise HTTPException(404, "Líder não encontrado")
    return {"ok": True}


@app.delete("/api/admin/lideres/{lid}", dependencies=[Depends(admin)])
def remover_lider(lid: int, con=Depends(db)):
    """Remove um líder e todas as caravanas dele."""
    con.execute("DELETE FROM caravanas WHERE lider_id=?", (lid,))
    if con.execute("DELETE FROM lideres WHERE id=?", (lid,)).rowcount == 0:
        raise HTTPException(404, "Líder não encontrado")
    return {"ok": True}


@app.put("/api/admin/capacidade", dependencies=[Depends(admin)])
def set_capacidade(body: CapacidadeIn, con=Depends(db)):
    """Define a capacidade de referência por turno."""
    con.execute("INSERT INTO config(chave,valor) VALUES('capacidade_turno',?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", (str(body.capacidade_turno),))
    return {"capacidade_turno": body.capacidade_turno}


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static")), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"))
