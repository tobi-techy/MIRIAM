# Workflow

- Reads reference documentation before implementing an unfamiliar integration. Confidence: 0.8
- Delivers incrementally with tight scope ("don't boil the ocean"); ships the smallest live slice first and measures before expanding. Confidence: 0.9
- Uses feature flags with a safe fallback to existing behavior, and logs when the feature is off or degraded. Confidence: 0.85
- Prefers feature flags to default to enabled (opt-out) rather than disabled (opt-in), with graceful fallback when the required credential/key is absent. Confidence: 0.7
- Treats changes to criteria/policy as product changes: version them and re-evaluate against a frozen eval set. Confidence: 0.8
- Values explicit non-goals and acceptance criteria to bound scope and define "done". Confidence: 0.8
- Does not assume US English: the product's users are often Nigeria/West Africa (pidgin/Naija English, Yoruba, Igbo, Hausa). Confidence: 0.8
- Uses VS Code with Pylance and expects editor type-checking (Pylance/pyright) to resolve against the project's uv `.venv`, not just CLI checks passing. Confidence: 0.7
- Reports honestly and without congratulation: never fabricates answers or metrics; stops and reports missing prerequisites (e.g. an API key) rather than inventing results; calls mediocre work mediocre. Confidence: 0.85
- Distinguishes BLOCKED (missing product policy requiring a human decision) from FAIL (wrong behavior) and names the decision a human must make. Confidence: 0.75
- Requires an explicit ship/hold decision per area against a quantified bar (e.g. >=90% pass rate and zero misses on safety-critical cases). Confidence: 0.75
- Reports top failures grouped by failure mode, each with product impact and a recommended fix (catalog/threshold/code). Confidence: 0.7
- Sets a quantified completion target (e.g. 99% success rate) and iterates fix → full-suite rerun until it is met, rather than stopping at "no obvious errors". Confidence: 0.7
- Bounds iteration on a fix: after the first full pass, at most one more criteria/threshold tweak and one more full rerun, then stop. Confidence: 0.7
