-- Caravanas do Templo — esquema do Supabase
-- Cole TUDO no SQL Editor do Supabase e execute. Pode ser executado de novo sem problema.

create extension if not exists pgcrypto with schema extensions;

-- ---------- tabelas ----------
create table if not exists public.perfis (
  id      uuid primary key references auth.users(id) on delete cascade,
  role    text not null check (role in ('admin','lider')),
  nome    text not null,
  usuario text not null unique,
  origem  text not null default '',
  tipo    text not null default 'estaca' check (tipo in ('estaca','ala'))
);

create table if not exists public.config (
  chave text primary key,
  valor text not null
);
insert into public.config(chave, valor) values ('capacidade_turno', '224') on conflict do nothing;

create table if not exists public.caravanas (
  id              bigint generated always as identity primary key,
  lider_id        uuid not null references public.perfis(id) on delete cascade,
  data            date not null,
  turno           text not null check (turno in ('manha','tarde')),
  com_investidura int  not null default 0 check (com_investidura between 0 and 1000),
  sem_investidura int  not null default 0 check (sem_investidura between 0 and 1000),
  pessoas         int  generated always as (com_investidura + sem_investidura) stored,
  transporte      text,
  obs             text,
  unidade         text not null,
  tipo_unidade    text not null check (tipo_unidade in ('estaca','ala')),
  created_at      timestamptz not null default now(),
  check (extract(isodow from data) = 6)
);
create index if not exists caravanas_data_turno on public.caravanas(data, turno);

-- ---------- segurança (RLS) ----------
create or replace function public.meu_papel() returns text
language sql stable security definer set search_path = public as
$$ select role from public.perfis where id = auth.uid() $$;

alter table public.perfis    enable row level security;
alter table public.config    enable row level security;
alter table public.caravanas enable row level security;

drop policy if exists perfis_ver on public.perfis;
create policy perfis_ver on public.perfis for select to authenticated
  using (id = auth.uid() or public.meu_papel() = 'admin');

drop policy if exists config_ver on public.config;
create policy config_ver on public.config for select to authenticated
  using (public.meu_papel() is not null);

drop policy if exists caravanas_ver on public.caravanas;
create policy caravanas_ver on public.caravanas for select to authenticated
  using (lider_id = auth.uid() or public.meu_papel() = 'admin');

-- Nenhuma política de escrita: todas as alterações passam pelas funções abaixo.
revoke all on public.perfis, public.config, public.caravanas from anon, authenticated;
grant select on public.perfis, public.config, public.caravanas to authenticated;

-- ---------- funções ----------
create or replace function public.exigir_admin() returns void
language plpgsql stable security definer set search_path = public as $$
begin
  if public.meu_papel() is distinct from 'admin' then
    raise exception 'Acesso de administrador negado';
  end if;
end $$;

-- Caravanas do mês, visíveis a qualquer usuário logado (sem dados pessoais).
create or replace function public.calendario_mes(p_mes text)
returns table(data date, turno text, unidade text, tipo text, pessoas int, com int, sem int, transporte text)
language plpgsql stable security definer set search_path = public as $$
declare ini date;
begin
  if public.meu_papel() is null then raise exception 'Sessão inválida ou expirada'; end if;
  if p_mes !~ '^\d{4}-\d{2}$' then raise exception 'Use o formato YYYY-MM'; end if;
  ini := (p_mes || '-01')::date;
  return query
    select c.data, c.turno, c.unidade, c.tipo_unidade, c.pessoas, c.com_investidura, c.sem_investidura, c.transporte
    from public.caravanas c
    where c.data >= ini and c.data < (ini + interval '1 month')::date
    order by c.data, c.turno;
end $$;

-- Cria ou edita uma caravana do líder logado. Garante sábado e limite de vagas.
create or replace function public.salvar_caravana(
  p_id bigint, p_data date, p_turno text, p_com int, p_sem int,
  p_transporte text, p_tipo text, p_unidade text
) returns bigint
language plpgsql security definer set search_path = public as $$
declare v public.perfis; cap int; usado int; novo bigint; com int := coalesce(p_com,0); sem int := coalesce(p_sem,0);
begin
  select * into v from public.perfis where id = auth.uid();
  if v.id is null or v.role <> 'lider' then raise exception 'Apenas líderes de caravana podem cadastrar'; end if;
  if p_data is null or extract(isodow from p_data) <> 6 then raise exception 'Caravanas só podem ser cadastradas para sábados'; end if;
  if p_turno not in ('manha','tarde') then raise exception 'Turno inválido'; end if;
  if com < 0 or sem < 0 or com > 1000 or sem > 1000 then raise exception 'Quantidade inválida'; end if;
  if com + sem < 1 then raise exception 'Informe ao menos uma pessoa'; end if;
  if coalesce(p_tipo, v.tipo) not in ('estaca','ala') then raise exception 'Tipo de unidade inválido'; end if;

  perform pg_advisory_xact_lock(hashtext(p_data::text || p_turno));

  if p_id is not null then
    perform 1 from public.caravanas where id = p_id and lider_id = v.id;
    if not found then raise exception 'Caravana não encontrada'; end if;
  end if;

  select valor::int into cap from public.config where chave = 'capacidade_turno';
  select coalesce(sum(pessoas), 0) into usado from public.caravanas
   where data = p_data and turno = p_turno and id is distinct from p_id;
  if usado + com + sem > cap then
    raise exception 'Este turno só tem % vaga(s) disponível(is) (capacidade %). Reduza o número de pessoas ou escolha outro turno/sábado.',
      greatest(0, cap - usado), cap;
  end if;

  if p_id is null then
    insert into public.caravanas(lider_id, data, turno, com_investidura, sem_investidura, transporte, unidade, tipo_unidade)
    values (v.id, p_data, p_turno, com, sem, nullif(p_transporte,''),
            coalesce(nullif(trim(p_unidade),''), v.origem), coalesce(p_tipo, v.tipo))
    returning id into novo;
    return novo;
  end if;
  update public.caravanas set data = p_data, turno = p_turno, com_investidura = com, sem_investidura = sem,
         transporte = nullif(p_transporte,''), unidade = coalesce(nullif(trim(p_unidade),''), v.origem),
         tipo_unidade = coalesce(p_tipo, v.tipo)
   where id = p_id;
  return p_id;
end $$;

create or replace function public.apagar_caravana(p_id bigint) returns void
language plpgsql security definer set search_path = public as $$
begin
  delete from public.caravanas where id = p_id and lider_id = auth.uid()
    and public.meu_papel() = 'lider';
  if not found then raise exception 'Caravana não encontrada'; end if;
end $$;

create or replace function public.definir_capacidade(p_cap int) returns void
language plpgsql security definer set search_path = public as $$
begin
  perform public.exigir_admin();
  if p_cap is null or p_cap < 1 then raise exception 'Capacidade inválida'; end if;
  insert into public.config(chave, valor) values ('capacidade_turno', p_cap::text)
  on conflict (chave) do update set valor = excluded.valor;
end $$;

-- Passo 2 da criação de líder: o login (auth) é criado pelo app; aqui entra o perfil.
create or replace function public.criar_perfil(p_id uuid, p_nome text, p_usuario text, p_origem text, p_tipo text)
returns void
language plpgsql security definer set search_path = public as $$
begin
  perform public.exigir_admin();
  if p_tipo not in ('estaca','ala') then raise exception 'Tipo de unidade inválido'; end if;
  if coalesce(trim(p_nome),'') = '' or coalesce(trim(p_usuario),'') = '' or coalesce(trim(p_origem),'') = '' then
    raise exception 'Preencha nome, unidade e usuário';
  end if;
  update auth.users set email_confirmed_at = coalesce(email_confirmed_at, now()) where id = p_id;
  if not found then raise exception 'Login não encontrado'; end if;
  begin
    insert into public.perfis(id, role, nome, usuario, origem, tipo)
    values (p_id, 'lider', trim(p_nome), lower(trim(p_usuario)), trim(p_origem), p_tipo);
  exception when unique_violation then
    raise exception 'Usuário já existe';
  end;
end $$;

create or replace function public.descartar_login_sem_perfil(p_id uuid) returns void
language plpgsql security definer set search_path = public as $$
begin
  perform public.exigir_admin();
  delete from auth.users u where u.id = p_id and not exists (select 1 from public.perfis p where p.id = u.id);
end $$;

create or replace function public.redefinir_senha(p_id uuid, p_senha text) returns void
language plpgsql security definer set search_path = public, extensions as $$
begin
  perform public.exigir_admin();
  if length(coalesce(p_senha,'')) < 6 then raise exception 'A senha precisa ter ao menos 6 caracteres'; end if;
  update auth.users set encrypted_password = crypt(p_senha, gen_salt('bf')), updated_at = now()
   where id = p_id and exists (select 1 from public.perfis where id = p_id and role = 'lider');
  if not found then raise exception 'Líder não encontrado'; end if;
end $$;

create or replace function public.remover_lider(p_id uuid) returns void
language plpgsql security definer set search_path = public as $$
begin
  perform public.exigir_admin();
  if not exists (select 1 from public.perfis where id = p_id and role = 'lider') then
    raise exception 'Líder não encontrado';
  end if;
  delete from auth.users where id = p_id;   -- apaga também o perfil e as caravanas (cascade)
end $$;

do $$
declare f text;
begin
  foreach f in array array[
    'meu_papel()', 'exigir_admin()', 'calendario_mes(text)',
    'salvar_caravana(bigint,date,text,int,int,text,text,text)', 'apagar_caravana(bigint)',
    'definir_capacidade(int)', 'criar_perfil(uuid,text,text,text,text)',
    'descartar_login_sem_perfil(uuid)', 'redefinir_senha(uuid,text)', 'remover_lider(uuid)'
  ] loop
    execute format('revoke all on function public.%s from public, anon', f);
    execute format('grant execute on function public.%s to authenticated', f);
  end loop;
end $$;

-- ---------- administrador ----------
-- 1) No painel: Authentication > Users > Add user > "Create new user"
--    e-mail: admin@caravanas.app  | senha: (a sua)  | marque "Auto Confirm User".
-- 2) Rode este arquivo de novo (ou só o comando abaixo) para dar o papel de admin:
insert into public.perfis(id, role, nome, usuario, origem)
select id, 'admin', 'Administrador', 'admin', '' from auth.users where email = 'admin@caravanas.app'
on conflict (id) do nothing;
