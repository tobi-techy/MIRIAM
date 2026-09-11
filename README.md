# Miriam Financial Agent

A production-grade financial agent built in Python that:
- Understands financial data and provides insights
- Builds plans and strategies
- Automates money safely with user approval
- Maintains good memory of conversations and user history
- Is conversational and proactive
- Targets 99%+ reliability for production use

## Architecture Overview

This is a bounded modular monolith designed for production safety and scalability:

```
flowchart TD
    A[User Chat] --> B[Conversation Layer]
    B --> C[Agent Orchestrator]
    C --> D[Memory Service]
    C --> E[Reasoning Engine]
    E --> F[Safety & Policy Layer]
    F --> G[Financial Intelligence]
    G --> H[Integration Gateway]
    H --> I[Go Payment Service gRPC]
    H --> J[Plaid/ACH]
    H --> K[Crypto Wallets]
    F --> L[Audit & Compliance]
    D --> M[PostgreSQL + Vector Store]
    G --> N[Analytics Engine]
```

## Key Features

- **Safety First**: All money actions require explicit user approval
- **Immutable Audit Trail**: Financial actions are recorded and cannot be altered
- **Fail Closed**: System stops rather than guessing when components are degraded
- **Idempotency**: Every external action is safe to retry
- **Proactive**: Scheduled check-ins, anomaly alerts, plan reminders

## Deployment Status

Currently in Phase 1: Production Foundations
- [ ] Python service scaffolding
- [ ] Authentication and authorization
- [ ] Observability (logging, metrics, tracing)
- [ ] Database setup
- [ ] Vector memory layer
- [ ] Safety boundaries
