# Contribution Guidelines

## Setting Up Development Environment

### Prerequisites
- Python 3.11 or higher
- PostgreSQL 15+ with pgvector extension
- Redis 7+

### Quick Start (Docker)

```bash
# Start development environment with all services
make dev-up

# Run tests
make test

# Start the application
make run
```

### Local Development

1. Install dependencies:
   ```bash
   pip install -e .[dev]
   ```

2. Set up database:
   ```bash
   # Run migrations
   alembic upgrade head
   
   # Insert test data if needed
   ```

3. Set environment variables:
   ```bash
   export DATABASE_URL=postgresql://miriam:miriam_password@localhost:5432/miriam_dev
   export REDIS_URL=redis://localhost:6379/0
   export LOG_LEVEL=DEBUG
   ```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=miriam_agent --cov-report=html

# Run specific test module
pytest tests/test_auth.py
```

### Code Style

```bash
# Format code
black .
isort .

# Check code style
black --check .
isort --check-only .
ruff check .
mypy miriam_agent
```

## Development Workflow

1. **Create a new feature branch**
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feature/your-feature
   ```

2. **Implement your changes**
   - Follow existing code patterns
   - Add tests for new functionality
   - Update documentation if needed

3. **Run tests**
   ```bash
   pytest
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: your feature description"
   ```

5. **Push and create a pull request**

## Project Structure

```
miriam_agent/
├── api/                    # API layer
├── agents/                 # Agent implementations
├── auth/                   # Authentication
├── config/                 # Configuration
├── database/               # Database layer
├── memory/                 # Memory and state management
├── observability/          # Logging, metrics, tracing
├── safety/                 # Safety and policy enforcement
├── integrations/           # External service integrations
├── financial/              # Financial intelligence
└── core/                   # Core utilities
```

## Code Quality Standards

### Testing
- All new code must have corresponding tests
- Tests should cover edge cases and error conditions
- Use pytest fixtures for test setup

### Documentation
- All public APIs must have docstrings
- Update README.md for significant changes
- Add inline comments for complex logic

### Security
- All database queries must use parameterized statements
- Input validation must be performed at all boundaries
- Never expose sensitive information in logs

### Performance
- Implement proper caching strategies
- Use database indexes for frequently queried fields
- Monitor performance with Prometheus metrics

## Pull Request Checklist

- [ ] Code follows project style guidelines
- [ ] All tests pass
- [ ] Documentation updated if needed
- [ ] Security scan passes
- [ ] Performance benchmarks (if applicable)
- [ ] Code review comments addressed

## Getting Help

If you have questions about the codebase or need help with your changes:

1. Check the existing documentation in `/docs/`
2. Look at recent similar changes in the git history
3. Ask in the team's communication channel
4. Refer to the contribution guidelines above

## Spec Change Control

Miriam's behavioral specification is stored in `docs/miriam_spec/` and managed through version 1.2. All spec references in the codebase (e.g., "spec §5", "spec v1.1 §6") point to this canonical document. Changes to the spec require special attention because they affect how the agent behaves, what it can say, and how it makes decisions.

### Editing the Spec

1. **Version Bumps**
   - Minor changes (typos, clarifications): increase patch (e.g., 1.2 → 1.3)
   - New sections or behavioral rules: increase minor (e.g., 1.2 → 2.0)
   - Breaking changes: increase major (e.g., 1.2 → 2.0)

2. **Change Documentation**
   - Edit `docs/miriam_spec/miriam_spec_vX.Y.md`
   - Update `docs/miriam_spec/CHANGELOG.md` with:
     - Clear description of changes
     - Impact assessment
     - Required actions for downstream code
     - Reference to benchmark results

3. **Testing Requirements**
   - Run the full test suite including:
     - `make test` or `pytest`
     - `make lint` and `mypy miriam_agent`
     - The new spec-gate tests (`tests/test_miriam_spec.py`)
   - Ensure all 262+ existing tests still pass
   - Verify that spec examples still validate correctly

4. **Review Process**
   - Spec changes require a senior engineer review
   - Changes must be clearly justified with use-case examples
   - Behavioral impact must be documented
   - All references to the old version must be updated

### Spec Validation

The CI pipeline now includes a **spec-gate step** that runs:
- `tests/test_miriam_spec.py` - validates document structure and references
- `tests/test_spec_examples.py` - validates good/bad example pairs
- This ensures the spec never drifts from what the code expects

If spec-gate tests fail, PRs cannot be merged until the document is fixed.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
