# Testing

- Tests against recorded fixtures and never hits live APIs in CI. Confidence: 0.9
- Maintains a golden-set eval with frozen expected outputs/branches, re-run whenever criteria change. Confidence: 0.85
- Prefers adversarial, failure-focused QA: prove the integration actually controls behavior (not just produces correct labels) and surface real failures rather than "improving the vibe". Confidence: 0.8
- Writes eval cases as structured data (id, gate, state, expected branch/constraints, actual answers/branch, latency, tokens, request_id, pass) rather than prose or notebook notes. Confidence: 0.8
- Runs evals through the production call path — one batched request per gate/catalog — instead of testing questions in isolation and assuming the gate works. Confidence: 0.8
- After a failure, proposes one smallest change and reruns the whole suite; avoids patch-and-rerun of a single case or widening criteria to fit messy inputs. Confidence: 0.75
- Pins the hard ordering invariant with an executable test across the whole fixture set (e.g. no investment recommendation may appear before the buffer/debt rules fire) rather than trusting review to catch a violation — "if a fixture gets an investment recommendation before buffer/debt rules fire, the build is wrong". Confidence: 0.8
- Wants the spec's scenarios shipped as a named acceptance fixture pack, including degradation cases such as running correctly with the LLM killed, model-swap changing only wording, and each named guarantee (idempotent split, limit breach moves nothing, over-limit ask). Confidence: 0.8
- Pins layer-boundary invariants with structural tests (e.g. assert the deterministic layer imports no model/LLM and the narration/judgment layers define no money tool or registry access) so a violation cannot be added quietly. Confidence: 0.8
- Exercises the live request path end-to-end through the real HTTP endpoint (auth, validation, routing) rather than calling the handler directly, and adds tripwire/absence assertions that a superseded path is never invoked or no longer exists. Confidence: 0.75
- Pins cross-service API response contracts with a checked-in fixture the consumer repo can pin against: it records the required keys and the explicitly forbidden keys, carries verbatim payloads recorded from the live endpoint, and a test re-runs each scenario so the pin cannot rot. Confidence: 0.85
- Pins a safety-relevant default with a test asserting the shipped configured value (e.g. the unattended-action ceiling is 0), so the default cannot silently invert, and proves the intended behavior end-to-end — that a "hold" produces a confirmable challenge rather than a blanket refusal. Confidence: 0.75
- Runs the repo's own test invocation, not a default one — mirrors the Makefile/CI target (e.g. `go test -race` on the touched packages) and checks the changed files with the repo's formatter (gofmt) before declaring green. Confidence: 0.7
