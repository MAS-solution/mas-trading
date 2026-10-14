-- MAS Trading: datos solo visibles para usuarios logueados.
-- El scanner escribe con la service key (salta RLS); la pagina lee con la anon key + sesion.

create table if not exists snapshot (
  id int primary key,
  generado timestamptz not null,
  data jsonb not null
);

create table if not exists senales (
  id text primary key,
  par text not null,
  d int not null,            -- 1 compra, -1 venta
  tipo int not null,         -- entrada tipo 1/2/3 (fractales)
  tf_ob text not null,       -- OB de 4h o 1h donde se armo
  entrada double precision not null,
  sl double precision not null,
  tp double precision not null,
  abierta timestamptz not null,
  estado text not null,      -- abierta / ganancia / perdida
  r double precision,
  cierre timestamptz
);
create index if not exists senales_abierta on senales (abierta desc);

alter table snapshot enable row level security;
alter table senales enable row level security;

drop policy if exists "leer logueado" on snapshot;
create policy "leer logueado" on snapshot for select to authenticated using (true);
drop policy if exists "leer logueado" on senales;
create policy "leer logueado" on senales for select to authenticated using (true);

revoke all on snapshot, senales from anon;
