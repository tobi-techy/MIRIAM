# Money reference ops: keeping the local rates honest

`miriam_agent/money/reference.py` ships a **placeholder** table (all rows
`sourced=False`, pinned `REFERENCE_AS_OF`). The pipeline treats it honestly:

- every plan labels placeholder rows in `assumptions` (never reads as official)
- past `MAX_AGE_DAYS` (90) the table is **stale**: investing is refused, but the
  **savings plan still stands** (buffer, debt attack, cashflow run on the user's
  own numbers + `MONEY_DEBT_*` settings bands)

## Going live (no code change)

Set env vars; `reference_from_env()` builds a `sourced=True` override that
`lookup(..., overrides=...)` prefers over the table:

```bash
MONEY_REF_COUNTRY=NG
MONEY_REF_CURRENCY=NGN
MONEY_REF_INFLATION_PCT=24.0        # e.g. NBS CPI print
MONEY_REF_RISK_FREE_PCT=19.0        # e.g. CBN MPR / NTB stop rate
MONEY_REF_FIRE_APR_PCT=25.0         # optional; falls back to settings
MONEY_REF_JUDGMENT_APR_PCT=10.0     # optional; falls back to settings
MONEY_REF_AS_OF=2026-09-24          # the print date, not today
MONEY_REF_SOURCE="NBS CPI Aug 2026; CBN MPR Sep 2026"
```

Unset/incomplete vars -> `None` -> placeholder table keeps working. Refresh
monthly (or when the central bank prints); staleness is computed from
`MONEY_REF_AS_OF`, so a fresh date re-enables investing verdicts.

## Wiring a real feed later

Have the Go host or a cron job write these vars / inject a `CountryReference`
via `lookup(country, overrides=ref)`. Python never fetches macro data itself.
