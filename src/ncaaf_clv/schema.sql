-- ncaaf-clv SQLite schema. Every table carries snapshot_utc for point-in-time joins.
-- Spread convention (betting-data-normalizing): spread_magnitude >= 0 + favorite side/team;
-- home_spread / away_spread are the signed perspective lines (negative = favourite).

PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------------ ESPN dimensions
CREATE TABLE IF NOT EXISTS espn_teams (
    team_id TEXT NOT NULL,
    snapshot_utc TEXT NOT NULL,
    uid TEXT, slug TEXT, location TEXT, nickname TEXT, abbreviation TEXT,
    display_name TEXT, short_display_name TEXT, color TEXT, alternate_color TEXT, logo TEXT,
    is_active INTEGER, is_program INTEGER,
    conference_group_id TEXT, conference_name TEXT, conference_abbr TEXT,
    division_group_id TEXT, parent_group_id TEXT, fbs_group_id TEXT, standing_summary TEXT,
    PRIMARY KEY (team_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_conferences (
    group_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    name TEXT, abbreviation TEXT, parent_group_id TEXT,
    PRIMARY KEY (group_id, snapshot_utc)
);

-- ------------------------------------------------------------------ ESPN games
CREATE TABLE IF NOT EXISTS espn_games (
    game_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    uid TEXT, name TEXT, short_name TEXT, kickoff_utc TEXT, time_valid INTEGER,
    season_year INTEGER, season_type INTEGER, week INTEGER,
    neutral_site INTEGER, conference_game INTEGER, attendance INTEGER,
    home_team_id TEXT, away_team_id TEXT, home_score INTEGER, away_score INTEGER,
    broadcast TEXT, notes TEXT,
    status_id TEXT, status_name TEXT, status_state TEXT, status_completed INTEGER,
    status_detail TEXT, clock REAL, display_clock TEXT, period INTEGER,
    venue_id TEXT, venue_name TEXT, venue_city TEXT, venue_state TEXT, venue_country TEXT,
    venue_indoor INTEGER, venue_grass INTEGER,
    PRIMARY KEY (game_id, snapshot_utc)
);
CREATE INDEX IF NOT EXISTS idx_espn_games_kickoff ON espn_games (kickoff_utc);

CREATE TABLE IF NOT EXISTS espn_game_teams (
    game_id TEXT NOT NULL, team_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    home_away TEXT, score INTEGER, winner INTEGER, curated_rank INTEGER, rank INTEGER,
    linescores TEXT,  -- JSON array of period scores
    record_overall TEXT, record_home TEXT, record_away TEXT, record_conf TEXT,
    team_uid TEXT, team_slug TEXT, team_location TEXT, team_nickname TEXT, team_abbreviation TEXT,
    team_display_name TEXT, team_short_display_name TEXT, team_color TEXT, team_alternate_color TEXT, team_logo TEXT,
    PRIMARY KEY (game_id, team_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_odds (
    game_id TEXT NOT NULL, provider_id TEXT, snapshot_utc TEXT NOT NULL,
    provider_name TEXT, details TEXT, home_team_id TEXT, away_team_id TEXT, favorite_team_id TEXT,
    spread_magnitude REAL, home_spread REAL, away_spread REAL, total REAL,
    home_ml INTEGER, away_ml INTEGER, home_spread_price INTEGER, away_spread_price INTEGER,
    over_price INTEGER, under_price INTEGER,
    open_home_spread REAL, open_total REAL, open_home_ml INTEGER, open_away_ml INTEGER,
    close_home_spread REAL, close_total REAL,
    PRIMARY KEY (game_id, provider_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_scoring_plays (
    game_id TEXT NOT NULL, play_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    team_id TEXT, period INTEGER, clock TEXT, clock_seconds REAL, play_type TEXT, scoring_type TEXT,
    text TEXT, away_score INTEGER, home_score INTEGER,
    PRIMARY KEY (game_id, play_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_game_leaders (
    game_id TEXT NOT NULL, team_id TEXT NOT NULL, category TEXT NOT NULL, rank INTEGER NOT NULL, snapshot_utc TEXT NOT NULL,
    player_id TEXT, player_name TEXT, position TEXT, display_value TEXT, value REAL,
    PRIMARY KEY (game_id, team_id, category, rank, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_win_probability (
    game_id TEXT NOT NULL, seq INTEGER NOT NULL, snapshot_utc TEXT NOT NULL,
    play_id TEXT, home_win_pct REAL, tie_pct REAL,
    PRIMARY KEY (game_id, seq, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_boxscore_team_stats (
    game_id TEXT NOT NULL, team_id TEXT NOT NULL, stat_name TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    home_away TEXT, stat_label TEXT, display_value TEXT, value REAL,
    PRIMARY KEY (game_id, team_id, stat_name, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_boxscore_player_stats (
    game_id TEXT NOT NULL, team_id TEXT NOT NULL, player_id TEXT NOT NULL,
    category TEXT NOT NULL, stat_key TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    player_name TEXT, jersey TEXT, stat_label TEXT, display_value TEXT, value REAL,
    PRIMARY KEY (game_id, team_id, player_id, category, stat_key, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_drives (
    game_id TEXT NOT NULL, drive_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    drive_order INTEGER, team_id TEXT, description TEXT, result TEXT, display_result TEXT,
    is_score INTEGER, yards INTEGER, offensive_plays INTEGER, time_elapsed TEXT,
    start_period INTEGER, start_clock TEXT, start_yard_line INTEGER, start_text TEXT,
    end_period INTEGER, end_clock TEXT, end_yard_line INTEGER, end_text TEXT,
    PRIMARY KEY (game_id, drive_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_plays (
    game_id TEXT NOT NULL, play_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    drive_id TEXT, sequence_number INTEGER, period INTEGER, clock TEXT, offense_team_id TEXT,
    play_type_id TEXT, play_type TEXT, text TEXT, away_score INTEGER, home_score INTEGER,
    scoring_play INTEGER, is_penalty INTEGER, is_turnover INTEGER, stat_yardage INTEGER,
    start_down INTEGER, start_distance INTEGER, start_yard_line INTEGER, start_yards_to_endzone INTEGER,
    start_possession_team_id TEXT,
    end_down INTEGER, end_distance INTEGER, end_yard_line INTEGER, end_yards_to_endzone INTEGER,
    end_possession_team_id TEXT, down_distance_text TEXT, wallclock_utc TEXT,
    PRIMARY KEY (game_id, play_id, snapshot_utc)
);
CREATE INDEX IF NOT EXISTS idx_espn_plays_game ON espn_plays (game_id, period, sequence_number);

-- ------------------------------------------------------------------ ESPN team pages
CREATE TABLE IF NOT EXISTS espn_team_season_stats (
    team_id TEXT NOT NULL, season_year INTEGER, side TEXT NOT NULL, category TEXT NOT NULL,
    stat_name TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    season_type INTEGER, stat_abbr TEXT, display_name TEXT, value REAL, display_value TEXT,
    per_game_value REAL, rank INTEGER,
    PRIMARY KEY (team_id, side, category, stat_name, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_team_schedule (
    team_id TEXT NOT NULL, game_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    name TEXT, short_name TEXT, kickoff_utc TEXT, time_valid INTEGER,
    season_year INTEGER, season_type INTEGER, week INTEGER, home_away TEXT, neutral_site INTEGER,
    opponent_team_id TEXT, opponent_display_name TEXT, team_score INTEGER, opponent_score INTEGER,
    winner INTEGER, curated_rank INTEGER,
    status_id TEXT, status_name TEXT, status_state TEXT, status_completed INTEGER, status_detail TEXT,
    clock REAL, display_clock TEXT, period INTEGER,
    venue_id TEXT, venue_name TEXT, venue_city TEXT, venue_state TEXT, venue_country TEXT,
    venue_indoor INTEGER, venue_grass INTEGER,
    PRIMARY KEY (team_id, game_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_roster (
    team_id TEXT NOT NULL, player_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    season_year INTEGER, uid TEXT, first_name TEXT, last_name TEXT, display_name TEXT, jersey TEXT,
    roster_group TEXT, position TEXT, position_name TEXT, position_group TEXT,
    height_in REAL, weight_lb REAL, experience_years INTEGER, class_year TEXT,
    birth_city TEXT, birth_state TEXT, birth_country TEXT, status TEXT,
    injury_status TEXT, injury_detail TEXT,
    PRIMARY KEY (team_id, player_id, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS espn_coaches (
    team_id TEXT NOT NULL, coach_id TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    season_year INTEGER, first_name TEXT, last_name TEXT, experience_years INTEGER,
    PRIMARY KEY (team_id, coach_id, snapshot_utc)
);

-- ------------------------------------------------------------------ Overtime.ag odds
CREATE TABLE IF NOT EXISTS overtime_odds (
    ot_game_num TEXT NOT NULL, period_key TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    captured_at TEXT, ot_parent_game_num TEXT, sport_sub_type_id INTEGER,
    period_number INTEGER, period_description TEXT,
    away_team_raw TEXT, home_team_raw TEXT, away_rot INTEGER, home_rot INTEGER,
    kickoff_local TEXT, kickoff_tz TEXT, kickoff_utc TEXT,
    favorite_side TEXT, favorite_team_raw TEXT,
    spread_magnitude REAL, home_spread REAL, away_spread REAL,
    away_spread_price INTEGER, home_spread_price INTEGER,
    total REAL, over_price INTEGER, under_price INTEGER,
    away_ml INTEGER, home_ml INTEGER,
    away_team_total REAL, away_tt_over_price INTEGER, away_tt_under_price INTEGER,
    home_team_total REAL, home_tt_over_price INTEGER, home_tt_under_price INTEGER,
    line_seq INTEGER, status TEXT, spread_status TEXT, ml_status TEXT, total_status TEXT,
    team_total_status TEXT, schedule_comment TEXT, wager_cutoff_utc TEXT,
    PRIMARY KEY (ot_game_num, period_key, snapshot_utc)
);
CREATE INDEX IF NOT EXISTS idx_overtime_odds_kickoff ON overtime_odds (kickoff_utc);

-- ------------------------------------------------------------------ joins
CREATE TABLE IF NOT EXISTS game_map (
    ot_game_num TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    espn_game_id TEXT, away_team_id TEXT, home_team_id TEXT, method TEXT,
    orientation_swapped INTEGER, kickoff_delta_minutes REAL,
    ot_kickoff_utc TEXT, espn_kickoff_utc TEXT, away_resolution TEXT, home_resolution TEXT,
    notes TEXT,  -- JSON array
    PRIMARY KEY (ot_game_num, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS team_map (
    source TEXT NOT NULL, raw_name TEXT NOT NULL, snapshot_utc TEXT NOT NULL,
    team_id TEXT, espn_display_name TEXT, method TEXT, candidates TEXT,
    PRIMARY KEY (source, raw_name, snapshot_utc)
);

CREATE TABLE IF NOT EXISTS validation_checks (
    snapshot_utc TEXT NOT NULL, name TEXT NOT NULL,
    level TEXT, description TEXT, passed INTEGER, observed INTEGER, failure_count INTEGER,
    failures TEXT,  -- JSON array
    PRIMARY KEY (snapshot_utc, name)
);

-- Convenience view: Overtime full-game line joined to the ESPN game + DraftKings reference.
CREATE VIEW IF NOT EXISTS v_game_lines AS
SELECT
    o.snapshot_utc, o.ot_game_num, m.espn_game_id, g.name AS espn_game_name, g.kickoff_utc,
    g.status_name, o.period_key, o.away_team_raw, o.home_team_raw, m.away_team_id, m.home_team_id,
    o.favorite_side, o.spread_magnitude, o.home_spread AS ot_home_spread, o.total AS ot_total,
    o.home_ml AS ot_home_ml, o.away_ml AS ot_away_ml,
    e.home_spread AS espn_home_spread, e.total AS espn_total, e.provider_name AS espn_provider,
    g.home_score, g.away_score
FROM overtime_odds o
JOIN game_map m ON m.ot_game_num = o.ot_game_num AND m.snapshot_utc = o.snapshot_utc
LEFT JOIN espn_games g ON g.game_id = m.espn_game_id
LEFT JOIN espn_odds e ON e.game_id = m.espn_game_id AND e.snapshot_utc = g.snapshot_utc;
