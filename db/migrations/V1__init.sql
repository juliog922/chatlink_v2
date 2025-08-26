CREATE TABLE IF NOT EXISTS users (
  id    SERIAL PRIMARY KEY,
  phone TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL UNIQUE,
  name  TEXT,
  role  TEXT NOT NULL CHECK (role IN ('admin','user'))
);

CREATE TABLE IF NOT EXISTS messages (
  id           BIGSERIAL PRIMARY KEY,
  client_id    INTEGER NOT NULL,
  client_phone TEXT,
  user_id      INTEGER,
  user_phone   TEXT,
  sender       TEXT,
  direction    TEXT NOT NULL CHECK (direction IN ('received','sent')),
  type         TEXT,
  content      TEXT,
  "timestamp"  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
