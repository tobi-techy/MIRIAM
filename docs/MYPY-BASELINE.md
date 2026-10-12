# Mypy baseline

`mypy miriam_agent` reports a large number of pre-existing type errors. Gating
CI on zero errors would mean the type check can never pass, so nothing is
checked at all. Instead the CI step runs a ratchet:

```console
python scripts/mypy_ratchet.py            # check (exit 1 on a new error)
python scripts/mypy_ratchet.py --update   # tighten after fixing errors
python scripts/mypy_ratchet.py --write    # record errors that are out of scope
```

The script compares the current run against `mypy-baseline.json` and fails only
when an error appears that the baseline does not already contain. Matching is on
`(path, rule, message)` and not on the line number, so moving code does not
manufacture phantom "new" errors; the baseline is a multiset, so a duplicate of
a known error is still new.

## Current state

The baseline is a snapshot of ~150 errors, dominated by:

- `database/models.py` — SQLAlchemy declarative models (`Base` is not valid as a
  type without the mapped-class typing plugin).
- Mixin attribute resolution in `hands/settlement.py`, `hands/settle_legs.py`
  (attributes only exist once the mixins are composed into `Orchestrator`).
- `no-any-return` on untyped provider/dependency boundaries.

## Follow-up

Fully typing the tree is tracked separately; that work is behavioral-neutral but
touches many files, so it is not bundled with the CI-pipeline fix. Land it
incrementally and call `--update` after each batch so the ratchet only shrinks.
