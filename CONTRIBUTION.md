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

## License

This project is licensed under the MIT License - see the LICENSE file for details.
