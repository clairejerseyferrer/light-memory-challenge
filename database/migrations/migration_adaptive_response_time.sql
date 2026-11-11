-- Adaptive difficulty v2: response-time allowance only (sequence length is NOT adaptive).
-- ADDITIVE and re-runnable. Run AFTER migration_adaptive_difficulty.sql (already applied).
-- Does not drop tables, delete rows, or rewrite existing game_sessions / game_results /
-- game_adaptive_events data. Rows written by the earlier design keep policy_version = NULL.

alter table public.game_adaptive_events
    add column if not exists policy_version text,                       -- 'response-time-v2' for new rows; NULL = legacy design
    add column if not exists classification text,                       -- Excellent / Good / Average / Struggling
    add column if not exists recommended_adjustment_ms integer,         -- step the IS recommended this level (+ eases, - tightens)
    add column if not exists previous_adjustment_ms integer,            -- cumulative adjustment before this decision
    add column if not exists recommended_new_adjustment_ms integer,     -- cumulative adjustment the IS recommended
    add column if not exists previous_response_time_allowance_ms integer,        -- limit used during the completed level
    add column if not exists recommended_response_time_allowance_ms integer,     -- limit recommended for the next level
    add column if not exists actual_response_time_adjustment_ms integer,         -- adjustment the ESP32 really applied
    add column if not exists applied_at timestamptz,
    add column if not exists applied_matches_recommendation boolean,             -- recommended == actual
    add column if not exists sequence_progression_verified boolean;              -- next sequence length == completed length + 1

-- Level 20 uses 22 buttons (3 + 19), so the old 2..20 bound on the OBSERVED length was too tight.
-- actual_sequence_length is now purely an observation, used to verify the fixed progression.
alter table public.game_adaptive_events
    drop constraint if exists game_adaptive_events_actual_sequence_length_check;
alter table public.game_adaptive_events
    add constraint game_adaptive_events_actual_sequence_length_check
    check (actual_sequence_length is null or actual_sequence_length between 2 and 30);

alter table public.game_adaptive_events
    drop constraint if exists game_adaptive_events_v2_ranges_check;
alter table public.game_adaptive_events
    add constraint game_adaptive_events_v2_ranges_check check (
        (recommended_new_adjustment_ms is null or recommended_new_adjustment_ms between -4000 and 10000)
        and (previous_adjustment_ms is null or previous_adjustment_ms between -4000 and 10000)
        and (actual_response_time_adjustment_ms is null or actual_response_time_adjustment_ms between -4000 and 10000)
        and (recommended_response_time_allowance_ms is null or recommended_response_time_allowance_ms between 8000 and 45000)
    );

create index if not exists idx_game_adaptive_events_policy
    on public.game_adaptive_events(policy_version);

comment on table public.game_adaptive_events is
'One immutable adaptive decision per session/level. v2 rows adapt the response-time allowance only; rows with NULL policy_version come from the earlier sequence-length design and are kept for history.';
