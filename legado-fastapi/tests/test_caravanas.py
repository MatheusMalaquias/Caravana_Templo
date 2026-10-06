import pytest
from fastapi.testclient import TestClient

SAB, SAB2, DOM = "2026-10-24", "2026-10-31", "2026-10-25"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADMIN_PASSWORD", "adm")
    from app.main import app
    c = TestClient(app)
    t = c.post("/api/login", json={"usuario": "admin", "senha": "adm"}).json()["token"]
    c.adm = {"Authorization": f"Bearer {t}"}
    return c


def lider(client, usuario, origem):
    client.post("/api/admin/lideres", headers=client.adm, json={"nome": usuario, "origem": origem, "usuario": usuario, "senha": "senha123"})
    t = client.post("/api/login", json={"usuario": usuario, "senha": "senha123"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def car(client, h, data=SAB, turno="manha", com=0, sem=0):
    return client.post("/api/caravanas", headers=h, json={"data": data, "turno": turno, "com_investidura": com, "sem_investidura": sem})


def test_soma_com_sem_e_nivel(client):
    client.put("/api/admin/capacidade", headers=client.adm, json={"capacidade_turno": 100})
    a, b = lider(client, "jf", "Juiz de Fora"), lider(client, "vr", "Volta Redonda")
    car(client, a, com=30, sem=10)
    car(client, b, com=40, sem=15)
    d = client.get("/api/calendario?mes=2026-10", headers=client.adm).json()["dias"][SAB]
    assert d["manha"]["total"] == 95 and d["manha"]["com"] == 70 and d["manha"]["sem"] == 25
    assert d["manha"]["nivel"] == "vermelho" and d["tarde"]["total"] == 0


def test_so_aceita_sabado(client):
    h = lider(client, "a", "A")
    assert car(client, h, data=DOM, com=5).status_code == 422
    assert car(client, h, data="2026-10-26", com=5).status_code == 422
    assert car(client, h, data=SAB, com=5).status_code == 201
    cid = car(client, h, data=SAB2, com=1).json()["id"]
    assert client.put(f"/api/caravanas/{cid}", headers=h, json={"data": DOM, "turno": "manha", "com_investidura": 1, "sem_investidura": 0}).status_code == 422


def test_precisa_de_ao_menos_uma_pessoa(client):
    h = lider(client, "a", "A")
    assert car(client, h).status_code == 422


def test_lider_nao_edita_caravana_de_outro(client):
    a, b = lider(client, "a", "A"), lider(client, "b", "B")
    cid = car(client, a, turno="tarde", sem=10).json()["id"]
    assert client.delete(f"/api/caravanas/{cid}", headers=b).status_code == 403
    assert client.delete(f"/api/caravanas/{cid}", headers=a).status_code == 200


def test_sem_token_e_admin_protegido(client):
    assert client.post("/api/caravanas", json={}).status_code == 401
    assert client.get("/api/calendario?mes=2026-10").status_code == 401
    h = lider(client, "a", "A")
    assert client.post("/api/admin/lideres", headers=h, json={}).status_code == 403
    assert car(client, client.adm, com=1).status_code == 403


def test_login_admin_senha_errada(client):
    assert client.post("/api/login", json={"usuario": "admin", "senha": "x"}).status_code == 401


def test_turno_invalido(client):
    h = lider(client, "a", "A")
    assert car(client, h, turno="noite", com=5).status_code == 422


def test_estacas_proximas_e_relatorio(client):
    a, b = lider(client, "jf", "Juiz de Fora"), lider(client, "vr", "Volta Redonda")
    car(client, a, com=20, sem=20)
    car(client, b, turno="tarde", com=5, sem=15)
    est = client.get("/api/admin/unidades", headers=client.adm).json()
    assert {e["unidade"]: e["pessoas"] for e in est} == {"Juiz de Fora": 40, "Volta Redonda": 20}
    r = client.get("/api/admin/relatorio?de=2026-10-01&ate=2026-10-31", headers=client.adm).json()
    assert r["total_pessoas"] == 60 and r["com_investidura"] == 25 and r["sem_investidura"] == 35
    assert r["por_turno"]["tarde"] == {"pessoas": 20, "com": 5, "sem": 15}
    csv = client.get("/api/admin/relatorio.csv?de=2026-10-01&ate=2026-10-31", headers=client.adm)
    assert csv.text.count("\n") == 3
    assert client.get("/api/admin/relatorio?de=2026-10-01&ate=2026-10-31", headers=a).status_code == 403


def test_senha_e_remocao_de_lider(client):
    h = lider(client, "a", "A")
    car(client, h, com=3)
    lid = client.get("/api/admin/lideres", headers=client.adm).json()[0]["id"]
    assert client.put(f"/api/admin/lideres/{lid}/senha", headers=client.adm, json={"senha": "nova1234"}).status_code == 200
    assert client.post("/api/login", json={"usuario": "a", "senha": "nova1234"}).status_code == 200
    assert client.post("/api/login", json={"usuario": "a", "senha": "senha123"}).status_code == 401
    assert client.delete(f"/api/admin/lideres/{lid}", headers=client.adm).status_code == 200
    assert client.get("/api/admin/lideres", headers=client.adm).json() == []


def test_lider_escolhe_estaca_ou_ala_por_caravana(client):
    h = lider(client, "jf", "Juiz de Fora")
    client.post("/api/caravanas", headers=h, json={"data": SAB, "turno": "manha", "com_investidura": 5, "sem_investidura": 0, "tipo_unidade": "ala", "unidade": "Ala Tijuca"})
    car(client, h, turno="tarde", com=3)
    d = client.get("/api/calendario?mes=2026-10", headers=client.adm).json()["dias"][SAB]
    assert d["manha"]["caravanas"][0]["origem"] == "Ala Tijuca" and d["manha"]["caravanas"][0]["tipo"] == "ala"
    assert d["tarde"]["caravanas"][0]["origem"] == "Juiz de Fora" and d["tarde"]["caravanas"][0]["tipo"] == "estaca"
    un = {(u["tipo"], u["unidade"]) for u in client.get("/api/admin/unidades", headers=client.adm).json()}
    assert un == {("ala", "Ala Tijuca"), ("estaca", "Juiz de Fora")}
    assert client.post("/api/caravanas", headers=h, json={"data": SAB, "turno": "manha", "com_investidura": 1, "tipo_unidade": "bairro"}).status_code == 422


def test_nao_ultrapassa_capacidade_do_turno(client):
    client.put("/api/admin/capacidade", headers=client.adm, json={"capacidade_turno": 100})
    a, b = lider(client, "a", "A"), lider(client, "b", "B")
    assert car(client, a, com=60, sem=30).status_code == 201
    r = car(client, b, com=10, sem=1)
    assert r.status_code == 409 and "10 vaga" in r.json()["detail"]
    assert car(client, b, com=10).status_code == 201          # completa exatamente 100
    assert car(client, b, turno="tarde", com=100).status_code == 201   # outro turno tem vagas próprias
    cid = car(client, b, data=SAB2, com=50).json()["id"]
    put = lambda n: client.put(f"/api/caravanas/{cid}", headers=b, json={"data": SAB2, "turno": "manha", "com_investidura": n, "sem_investidura": 0})
    assert put(100).status_code == 200 and put(101).status_code == 409   # edição ignora a própria caravana
    d = client.get("/api/calendario?mes=2026-10", headers=client.adm).json()["dias"][SAB]
    assert d["manha"]["vagas"] == 0
