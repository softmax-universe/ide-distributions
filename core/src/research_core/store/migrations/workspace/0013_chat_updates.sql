-- What a background wake told the user with ui.tell, waiting for the coordinator to hand it to
-- the Coworld's most recent chat. category is why the user should hear it now; only the
-- categories the chat_updates.categories setting lists are recorded here, the rest become
-- notices at once. A row is pending while admission_id and notice_id are both NULL:
-- admission_id names the chat admission that carried it, notice_id the notice it became
-- when the Coworld had no chat. Either receipt commits with that admission or notice.
CREATE TABLE chat_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coworld_id TEXT,
    category TEXT NOT NULL CHECK(category IN ('decision','outcome','submission','failure','merge')),
    text TEXT NOT NULL,
    refs TEXT NOT NULL DEFAULT '[]',
    created TEXT NOT NULL,
    created_by TEXT NOT NULL,
    admission_id TEXT REFERENCES client_admissions(op_id),
    notice_id INTEGER REFERENCES notices(id)
);
CREATE INDEX chat_updates_pending ON chat_updates(coworld_id, id)
    WHERE admission_id IS NULL AND notice_id IS NULL;
CREATE TRIGGER chat_updates_log_insert AFTER INSERT ON chat_updates BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'chat_updates',NEW.rowid,'insert');
END;
CREATE TRIGGER chat_updates_log_update AFTER UPDATE ON chat_updates BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'chat_updates',NEW.rowid,'update');
END;
CREATE TRIGGER chat_updates_log_delete AFTER DELETE ON chat_updates BEGIN
    INSERT INTO change_log(at,tbl,row_id,op)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ','now'),'chat_updates',OLD.rowid,'delete');
END;
