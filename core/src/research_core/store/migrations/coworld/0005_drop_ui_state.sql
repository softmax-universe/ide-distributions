-- The ui tool and UI state were removed (2026-11-12). The Coworld store's ui_state table was never
-- written (UI state lived in the workspace store); it and its change_log triggers go too.
DROP TRIGGER IF EXISTS ui_state_log_insert;
DROP TRIGGER IF EXISTS ui_state_log_update;
DROP TRIGGER IF EXISTS ui_state_log_delete;
DROP TABLE IF EXISTS ui_state;
