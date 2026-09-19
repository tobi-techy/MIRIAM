# Architecture

- Separates decision logic from language generation: typed/structured layers own the judgments code branches on; the LLM stays responsible for user-facing text. Confidence: 0.9
- Composes decisions in code (if-statements over typed scores) rather than prompting a model to weigh multiple factors and decide. Confidence: 0.9
- Keeps individual questions atomic and independent; avoids chaining reasoning across questions or assuming one answer is visible to another. Confidence: 0.85
- Distinguishes fail-closed (safety-critical: block on error) from fail-open (only with a documented fallback). Confidence: 0.85
- Avoids duplicating policy in two places; prefers a single source of truth and deleting superseded instructions. Confidence: 0.85
- Trims context/state sent to models: no embeddings, raw tool dumps, or entire documents; only the relevant slice. Confidence: 0.8
- Evaluates decision gates in a fixed, documented precedence order (block/escalate before clarify/planning) so safety checks are never masked by lower-priority checks like clarify or relevance. Confidence: 0.8
