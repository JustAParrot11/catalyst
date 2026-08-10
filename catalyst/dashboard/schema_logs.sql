-- Minimal logs table for the dashboard's searchable log view.
--
-- OWNERSHIP NOTE: storage/schema.sql is a shared file that routes
-- through a single coordinating session (ARCHITECTURE.md section 8), so
-- ui-designer does NOT edit it. This DDL lives here for the
-- orchestrator/integration session to fold into the main schema. It is
-- CREATE TABLE IF NOT EXISTS and safe to run twice.
--
-- Design notes:
-- - `context_json` holds the state at the time of an error, and
--   `traceback_text` the full traceback (BUILD-BRIEF: "Errors carry the
--   full traceback and the state at the time").
-- - Nothing here is trusted to be credential-free: the dashboard runs
--   every rendered log line and every diagnostic bundle through
--   catalyst.dashboard.redact, and writers are expected to redact at
--   capture as well. Two layers, because one is a promise.

CREATE TABLE IF NOT EXISTS logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,          -- ISO-8601 UTC
    level          TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    component      TEXT NOT NULL,          -- 'data.edgar', 'risk.evaluate', ...
    message        TEXT NOT NULL,
    cycle_id       TEXT,                   -- ties a line to one orchestrator cycle
    candidate_id   TEXT,                   -- ties a line to one decision trace
    traceback_text TEXT,                   -- full traceback on errors
    context_json   TEXT                    -- state at the time, JSON object
);

CREATE INDEX IF NOT EXISTS idx_logs_ts        ON logs (ts);
CREATE INDEX IF NOT EXISTS idx_logs_level_ts  ON logs (level, ts);
CREATE INDEX IF NOT EXISTS idx_logs_component ON logs (component, ts);
