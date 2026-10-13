-- not_null_preflight.sql — run BEFORE applying migration 003.
--
-- READ-ONLY. Counts the NULLs in every column migration 003 wants to make NOT
-- NULL. No writes, no DDL, no lock beyond ordinary SELECTs.
--
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/not_null_preflight.sql
--
-- Every row must show null_count = 0 for 003 to apply cleanly. A non-zero
-- null_count is the exact column 003 will refuse on — backfill it (the proposed
-- value per column is in docs/ALEMBIC-DRIFT-RECONCILIATION.md) or leave 003
-- unapplied. 003 never invents a value.
--
-- The statement is generated, not literal, so a table the deployed database does
-- not have yet (payment_references, which create_all never reached) is skipped
-- instead of erroring: on the deployed shape this lists 33 columns, and
-- payment_references.created_at appears once migration 001 has created it.

SELECT string_agg(
         format('SELECT %L AS null_column, count(*) AS null_count FROM %I WHERE %I IS NULL',
                t || '.' || c, t, c),
         E'\nUNION ALL\n' ORDER BY t, c
       ) || E'\nORDER BY null_count DESC, null_column;'
FROM (VALUES
    ('channel_identities', 'verified'),
    ('channel_identities', 'created_at'),
    ('channel_identities', 'last_seen_at'),
    ('budgets', 'current_spend'),
    ('budgets', 'created_at'),
    ('investments', 'purchase_date'),
    ('investments', 'acquired_at'),
    ('payment_references', 'created_at'),
    ('audit_logs', 'details'),
    ('audit_logs', 'created_at'),
    ('conversations', 'created_at'),
    ('conversations', 'updated_at'),
    ('messages', 'metadata'),
    ('messages', 'created_at'),
    ('users', 'created_at'),
    ('users', 'is_active'),
    ('users', 'preferences'),
    ('memory_entries', 'metadata'),
    ('memory_entries', 'created_at'),
    ('memory_entries', 'updated_at'),
    ('transactions', 'transaction_date'),
    ('transactions', 'created_at'),
    ('transactions', 'metadata'),
    ('financial_profiles', 'current_savings'),
    ('financial_profiles', 'risk_tolerance'),
    ('financial_profiles', 'investment_goals'),
    ('financial_profiles', 'financial_goals'),
    ('financial_profiles', 'created_at'),
    ('financial_profiles', 'updated_at'),
    ('tool_usage', 'parameters'),
    ('tool_usage', 'result'),
    ('tool_usage', 'created_at'),
    ('money_executions', 'created_at'),
    ('money_executions', 'updated_at')
) AS v(t, c)
WHERE to_regclass('public.' || quote_ident(t)) IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM information_schema.columns ic
    WHERE ic.table_schema = 'public' AND ic.table_name = v.t AND ic.column_name = v.c
  )
\gexec
