-- A chat's pending experiment results survive sleep and runtime replacement. Handoff to the
-- chat admission and the receipt on this row commit together in the workspace database.
CREATE TABLE xp_followups (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    experiment TEXT NOT NULL,
    request_ids TEXT NOT NULL,
    created_at TEXT NOT NULL,
    admission_id TEXT REFERENCES client_admissions(op_id)
);
CREATE INDEX xp_followups_pending ON xp_followups(created_at) WHERE admission_id IS NULL;
CREATE TRIGGER xp_followups_log_insert AFTER INSERT ON xp_followups BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'xp_followups',NEW.rowid,'insert');
END;
CREATE TRIGGER xp_followups_log_update AFTER UPDATE ON xp_followups BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'xp_followups',NEW.rowid,'update');
END;
CREATE TRIGGER xp_followups_log_delete AFTER DELETE ON xp_followups BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'xp_followups',OLD.rowid,'delete');
END;
