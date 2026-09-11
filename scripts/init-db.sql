-- Initial database schema for Miriam Agent (PostgreSQL 15 + pgvector).
-- Mirrors miriam_agent/database/models.py; created before first API start.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    id             VARCHAR PRIMARY KEY,
    username       VARCHAR UNIQUE NOT NULL,
    email          VARCHAR UNIQUE NOT NULL,
    full_name      VARCHAR NOT NULL,
    created_at     TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    is_active      BOOLEAN DEFAULT TRUE,
    preferences    JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS financial_profiles (
    id               VARCHAR PRIMARY KEY,
    user_id          VARCHAR NOT NULL REFERENCES users(id),
    monthly_income   DOUBLE PRECISION NOT NULL,
    current_savings  DOUBLE PRECISION DEFAULT 0,
    risk_tolerance   VARCHAR DEFAULT 'medium',
    investment_goals JSONB DEFAULT '[]'::jsonb,
    financial_goals  JSONB DEFAULT '[]'::jsonb,
    created_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS transactions (
    id                   VARCHAR PRIMARY KEY,
    financial_profile_id VARCHAR NOT NULL REFERENCES financial_profiles(id),
    amount               DOUBLE PRECISION NOT NULL,
    description          VARCHAR NOT NULL,
    category             VARCHAR NOT NULL,
    type                 VARCHAR NOT NULL,
    transaction_date     TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    metadata             JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS investments (
    id                   VARCHAR PRIMARY KEY,
    financial_profile_id VARCHAR NOT NULL REFERENCES financial_profiles(id),
    symbol               VARCHAR NOT NULL,
    name                 VARCHAR NOT NULL,
    quantity             DOUBLE PRECISION NOT NULL,
    purchase_price       DOUBLE PRECISION NOT NULL,
    current_price        DOUBLE PRECISION NOT NULL,
    purchase_date        TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    current_value        DOUBLE PRECISION NOT NULL,
    acquired_at          TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS budgets (
    id                   VARCHAR PRIMARY KEY,
    financial_profile_id VARCHAR NOT NULL REFERENCES financial_profiles(id),
    category             VARCHAR NOT NULL,
    monthly_limit        DOUBLE PRECISION NOT NULL,
    current_spend        DOUBLE PRECISION DEFAULT 0,
    created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS conversations (
    id         VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL REFERENCES users(id),
    title      VARCHAR NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS messages (
    id              VARCHAR PRIMARY KEY,
    conversation_id VARCHAR NOT NULL REFERENCES conversations(id),
    role            VARCHAR NOT NULL,
    content         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id         VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL REFERENCES users(id),
    type       VARCHAR NOT NULL,
    content    TEXT NOT NULL,
    metadata   JSONB DEFAULT '{}'::jsonb,
    embedding  VECTOR(1536),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_embedding
    ON memory_entries USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          VARCHAR PRIMARY KEY,
    user_id     VARCHAR NOT NULL REFERENCES users(id),
    action      VARCHAR NOT NULL,
    resource    VARCHAR NOT NULL,
    resource_id VARCHAR,
    details     JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE IF NOT EXISTS tool_usage (
    id             VARCHAR PRIMARY KEY,
    user_id        VARCHAR NOT NULL REFERENCES users(id),
    tool_name      VARCHAR NOT NULL,
    parameters     JSONB DEFAULT '{}'::jsonb,
    result         JSONB DEFAULT '{}'::jsonb,
    execution_time DOUBLE PRECISION NOT NULL,
    created_at     TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_created
    ON audit_logs (user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_tool_usage_user_created
    ON tool_usage (user_id, created_at);