-- The ui tool and every client route that read or wrote UI state were removed (2026-11-12): no
-- hosted client ever rendered it. Its table and change_log triggers go with it; the rows held
-- only panels, asks and reactions nobody reads.
DROP TRIGGER IF EXISTS ui_state_log_insert;
DROP TRIGGER IF EXISTS ui_state_log_update;
DROP TRIGGER IF EXISTS ui_state_log_delete;
DROP TABLE IF EXISTS ui_state;
