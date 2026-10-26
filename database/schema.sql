create sequence if not exists public.game_session_number_seq start with 1;
create table if not exists public.game_sessions (
 id uuid primary key,
 session_number bigint not null unique default nextval('public.game_session_number_seq'),
 codename varchar(24) not null check (char_length(trim(codename)) between 2 and 24),
 device_id text not null default 'ESP32-001',
 status text not null default 'waiting' check (status in ('waiting','countdown','playing','completed','abandoned')),
 created_at timestamptz not null default now(), start_at timestamptz, started_at timestamptz, completed_at timestamptz
);
create index if not exists idx_game_sessions_device_status on public.game_sessions(device_id,status,created_at desc);
create index if not exists idx_game_sessions_codename on public.game_sessions(lower(codename),created_at desc);
create table if not exists public.game_results (
 session_id uuid primary key references public.game_sessions(id) on delete cascade,
 score integer not null check (score >= 0), highest_level integer not null check (highest_level >= 1),
 mistakes integer not null check (mistakes >= 0), total_rounds integer not null check (total_rounds >= 0),
 correct_rounds integer not null check (correct_rounds >= 0 and correct_rounds <= total_rounds),
 accuracy numeric(5,2) not null check (accuracy between 0 and 100),
 game_duration_seconds numeric(10,2) not null check (game_duration_seconds >= 0),
 lives_remaining integer not null check (lives_remaining between 0 and 3),
 avg_response_time_ms numeric(10,2), fastest_response_time_ms numeric(10,2), slowest_response_time_ms numeric(10,2),
 round_data jsonb not null default '[]'::jsonb, analysis jsonb not null default '{}'::jsonb, saved_at timestamptz not null default now()
);
alter table public.game_sessions enable row level security;
alter table public.game_results enable row level security;
