# Testing

- Tests against recorded fixtures and never hits live APIs in CI. Confidence: 0.9
- Maintains a golden-set eval with frozen expected outputs/branches, re-run whenever criteria change. Confidence: 0.85
- Prefers adversarial, failure-focused QA: prove the integration actually controls behavior (not just produces correct labels) and surface real failures rather than "improving the vibe". Confidence: 0.8
- Writes eval cases as structured data (id, gate, state, expected branch/constraints, actual answers/branch, latency, tokens, request_id, pass) rather than prose or notebook notes. Confidence: 0.8
- Runs evals through the production call path — one batched request per gate/catalog — instead of testing questions in isolation and assuming the gate works. Confidence: 0.8
- After a failure, proposes one smallest change and reruns the whole suite; avoids patch-and-rerun of a single case or widening criteria to fit messy inputs. Confidence: 0.75
- Pins the hard ordering invariant with an executable test across the whole fixture set (e.g. no investment recommendation may appear before the buffer/debt rules fire) rather than trusting review to catch a violation — "if a fixture gets an investment recommendation before buffer/debt rules fire, the build is wrong". Confidence: 0.8
