# Caravanas do Templo
Calendário de ocupação do templo por dia e turno (manhã/tarde), alimentado pelos líderes de caravana.

```bash
pip install -r requirements.txt
cp .env.example .env   # ajuste ADMIN_PASSWORD e SECRET_KEY
set -a; source .env; set +a
uvicorn app.main:app --reload
```
Abra http://localhost:8000 (docs da API em /docs). O admin (botão "Admin") cria os líderes e define a capacidade por turno.
Testes: `python3 -m pytest`
