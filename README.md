# Caravanas do Templo

Agenda de ocupação do templo por sábado e turno (manhã/tarde), alimentada pelos líderes de caravana.
Página estática (GitHub Pages) + Supabase (login, banco e regras).

## Como publicar

1. **Supabase → SQL Editor:** cole e execute todo o arquivo [`supabase/schema.sql`](supabase/schema.sql).
2. **Supabase → Authentication → Users → Add user → Create new user:**
   e-mail `admin@caravanas.app`, uma senha forte e marque **Auto Confirm User**.
   Depois execute o `schema.sql` de novo (ele dá o papel de administrador a esse usuário).
3. **Supabase → Authentication → Providers → Email:** desligue **Confirm email**.
4. **Supabase → Project Settings → API:** copie a *Project URL* e a chave *anon public* para [`docs/config.js`](docs/config.js).
5. **GitHub → Settings → Pages:** *Deploy from a branch*, branch `main`, pasta `/docs`.

O login do administrador é o usuário `admin` e a senha do passo 2. Os líderes são criados na aba **Líderes**.

## Regras (aplicadas no banco, não só na tela)
- Líderes só cadastram caravanas aos **sábados** e só enxergam/editam as suas.
- Cada turno aceita no máximo a **capacidade** configurada (padrão 224).
- Todos os usuários logados veem a ocupação por dia, sem dados pessoais.

`legado-fastapi/` guarda a primeira versão, com servidor Python + SQLite (não é usada pelo GitHub Pages).
