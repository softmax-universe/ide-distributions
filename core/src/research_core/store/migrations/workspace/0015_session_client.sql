-- The client that opened the Session ({id, kind}), recorded at /session-open. Identity writes
-- (a cited name, met_user) are allowed only from a human IDE chat client; a Session with no
-- recorded client is refused. Existing rows stay NULL, which fails closed.
ALTER TABLE sessions ADD COLUMN client TEXT;
