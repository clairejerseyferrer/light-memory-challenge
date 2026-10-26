-- Run this in the EXISTING Supabase project. Preserves UUIDs and existing rows.
create sequence if not exists public.game_session_number_seq start with 1;
alter table public.game_sessions add column if not exists session_number bigint;
update public.game_sessions set session_number = nextval('public.game_session_number_seq') where session_number is null;
select setval('public.game_session_number_seq', coalesce((select max(session_number) from public.game_sessions), 0) + 1, false);
alter table public.game_sessions alter column session_number set default nextval('public.game_session_number_seq');
alter table public.game_sessions alter column session_number set not null;
create unique index if not exists game_sessions_session_number_unique on public.game_sessions(session_number);
