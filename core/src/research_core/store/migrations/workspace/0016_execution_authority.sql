ALTER TABLE sessions ADD COLUMN authority TEXT NOT NULL DEFAULT '{"mode":"owner"}';
ALTER TABLE client_admissions ADD COLUMN authority TEXT NOT NULL DEFAULT '{"mode":"owner"}';

CREATE TRIGGER sessions_authority_immutable
BEFORE UPDATE OF authority ON sessions
WHEN NEW.authority != OLD.authority
BEGIN
    SELECT RAISE(ABORT, 'session execution identity is immutable');
END;

CREATE TRIGGER client_admissions_authority_immutable
BEFORE UPDATE OF authority ON client_admissions
WHEN NEW.authority != OLD.authority
BEGIN
    SELECT RAISE(ABORT, 'admission execution authorization is immutable');
END;
