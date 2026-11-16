ALTER TABLE goals ADD COLUMN kind TEXT NOT NULL DEFAULT 'outcome'
    CHECK(kind IN ('outcome', 'ongoing'));
