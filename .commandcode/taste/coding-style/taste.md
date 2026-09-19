# Coding Style

- Prefers strong typing: Pydantic models and typed response models at call sites; explicitly avoids "dict soup". Confidence: 0.9
- Reads secrets and API keys from environment/config; never hardcodes credentials. Confidence: 0.9
- Centralizes magic numbers (thresholds, limits) in a dedicated policy/config module instead of scattering them through code. Confidence: 0.85
- Prefers SDK-provided typed primitives (question/answer classes) over hand-rolled JSON or strings. Confidence: 0.8
- Prefers async clients on the request/hot path. Confidence: 0.8
- Uses explicit retry policies on 429/5xx and honors Retry-After. Confidence: 0.75
- Logs structured observability around model calls: request id, catalog/version, tokens, latency, and the branch ultimately taken. Confidence: 0.85
- Logs the true deciding reason for a decision; a higher-severity reason (e.g. an authority block) must not be hidden behind a lower-severity one (e.g. a relevance reject). Confidence: 0.75
