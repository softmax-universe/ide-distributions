-- A Task can follow several experiments independently of its blocking wait.
CREATE TABLE task_xp_waits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    experiment TEXT NOT NULL,
    request_ids TEXT NOT NULL,
    UNIQUE (task_id, experiment)
);
CREATE TRIGGER task_xp_waits_log_insert AFTER INSERT ON task_xp_waits BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'task_xp_waits',NEW.rowid,'insert');
END;
CREATE TRIGGER task_xp_waits_log_update AFTER UPDATE ON task_xp_waits BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'task_xp_waits',NEW.rowid,'update');
END;
CREATE TRIGGER task_xp_waits_log_delete AFTER DELETE ON task_xp_waits BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'task_xp_waits',OLD.rowid,'delete');
END;
