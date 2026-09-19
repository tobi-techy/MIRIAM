# Architecture

- Separates decision logic from language generation: typed/structured layers own the judgments code branches on; the LLM stays responsible for user-facing text. Confidence: 0.9
- Composes decisions in code (if-statements over typed scores) rather than prompting a model to weigh multiple factors and decide. Confidence: 0.9
- Keeps individual questions atomic and independent; avoids chaining reasoning across questions or assuming one answer is visible to another. Confidence: 0.85
- Distinguishes fail-closed (safety-critical: block on error) from fail-open (only with a documented fallback). Confidence: 0.85
- Avoids duplicating policy in two places; prefers a single source of truth and deleting superseded instructions. Explicitly wants exactly one public entrypoint per capability ("if two entrypoints exist, kill one") and forbids starting a parallel/duplicate module — extend the existing package and say so when the placement is uncertain. Confidence: 0.85
- Never guesses an identifier or value that must come from a real external source (e.g. an onchain asset ID): resolve it from a live source or refuse outright, because a plausible-but-wrong value is more dangerous than an error. Confidence: 0.85
- For hand-entered external reference data (rates, inflation, thresholds), dates every row and labels it sourced vs PLACEHOLDER so a guess cannot read as official, and treats a table past its staleness window as unknown (fails closed) rather than producing a confident answer from stale figures. Confidence: 0.8
- Treats a bare confirmation ("do it", "go", "yes") as executing exactly the previously proposed action and nothing beyond it. Confidence: 0.75
- Trims context/state sent to models: no embeddings, raw tool dumps, or entire documents; only the relevant slice. Confidence: 0.8
- Evaluates decision gates in a fixed, documented precedence order (block/escalate before clarify/planning) so safety checks are never masked by lower-priority checks like clarify or relevance. Confidence: 0.8
- Represents missing inputs as absent rather than zero, labels every assumption explicitly, and lowers a confidence score instead of inventing a plausible number. Confidence: 0.8
- Keeps the agent out of irreversible or money-moving actions: it recommends, validates, and prepares a draft, while execution and enrollment stay user-signed and owned by a separate authority. Confidence: 0.75
