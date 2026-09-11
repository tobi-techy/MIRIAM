# Miriam Financial Agent - Complete Documentation

## Overview

Miriam Financial Agent is a production-grade financial AI assistant built in Python. It provides comprehensive financial intelligence, planning, and safe money automation with a conversational interface that feels like talking to a professional financial advisor like Ramit Sethi.

## Key Features

### Financial Intelligence
- Account analysis and insights
- Portfolio performance tracking
- Budget planning and forecasting
- Transaction categorization and analysis
- Financial advice and recommendations

### Safe Money Automation
- User-approved transactions
- Budget enforcement
- Investment strategy execution
- Proactive financial management
- Risk monitoring and alerts

### Memory and Personalization
- Conversational memory (short-term)
- Long-term financial profiles
- Goal tracking and progress monitoring
- Preference learning
- Pattern recognition

### Safety and Compliance
- Immutable audit trails
- Risk assessment and approval workflows
- Input validation and sanitization
- Compliance monitoring
- Security controls

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         API LAYER                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                   │
│  │    API      │  │   Chat      │  │  Auth       │                   │
│  │   Gateway   │  │   Endpoints │  │   System    │                   │
│  └─────────────┘  └─────────────┘  └─────────────┘                   │
└─────────────────┬─────────────────────────────────────────────────┘
                  │
┌─────────────────┼─────────────────────────────────────────────────┐
│                 │                                                 │
│    AGENT LAYER  │                                                 │
│                 │                                                 │
│  ┌─────────────┐ │  ┌─────────────┐  ┌─────────────┐              │
│  │   Agent     │ │  │  Financial │  │   Safety    │              │
│  │  Orchestrator│ │  │  Agent     │  │ & Policy    │              │
│  └─────────────┘ │  └─────────────┘  └─────────────┘              │
└─────────────────┴─────────────────────────────────────────────────┘
                  │
┌─────────────────┼─────────────────────────────────────────────────┐
│                 │                                                 │
│   DATA LAYER    │                                                 │
│                 │                                                 │
│  ┌─────────────┐ │  ┌─────────────┐  ┌─────────────┐              │
│  │  Database   │ │  │  Memory    │  │  Vector     │              │
│  │   Store     │ │  │  Store     │  │  Store      │              │
│  └─────────────┘ │  └─────────────┘  └─────────────┘              │
└─────────────────┴─────────────────────────────────────────────────┘
                  │
┌─────────────────┼─────────────────────────────────────────────────┐
│                 │                                                 │
│  INTEGRATION    │                                                 │
│   LAYER         │                                                 │
│                 │                                                 │
│  ┌─────────────┐ │  ┌─────────────┐  ┌─────────────┐              │
│  │   gRPC      │ │  │   Plaid    │  │   Crypto    │              │
│  │   Client    │ │  │   Integration│ │   Wallet    │              │
│  └─────────────┘ │  └─────────────┘  └─────────────┘              │
└─────────────────┴─────────────────────────────────────────────────┘
                  │
┌─────────────────┼─────────────────────────────────────────────────┐
│                 │                                                 │
│  OBSERVABILITY  │                                                 │
│   LAYER         │                                                 │
│                 │                                                 │
│  ┌─────────────┐ │  ┌─────────────┐  ┌─────────────┐              │
│  │  Logging    │ │  │  Metrics   │  │  Tracing    │              │
│  └─────────────┘ │  └─────────────┘  └─────────────┘              │
└─────────────────────────────────────────────────────────────────────┘
```

## Module Structure

### Core (`miriam_agent/core/`)
- **Models**: Core data models (User, FinancialProfile, Transaction, etc.)
- **Exceptions**: Custom exception hierarchy
- **Security**: Security utilities and encryption

### Auth (`miriam_agent/auth/`)
- **JWT**: JWT token handling
- **OAuth**: OAuth2 authentication
- **RBAC**: Role-based access control

### API (`miriam_agent/api/`)
- **Main**: FastAPI application setup
- **Chat**: Chat API endpoints
- **Dependencies**: FastAPI dependency injection

### Agents (`miriam_agent/agents/`)
- **Base**: Base agent class
- **Financial**: Financial agent implementation

### Database (`miriam_agent/database/`)
- **Models**: SQLAlchemy models
- **Memory**: Memory store implementation

### Financial (`miriam_agent/financial/`)
- **Intelligence**: Financial intelligence and analysis

### Integrations (`miriam_agent/integrations/`)
- **Base**: Base integration class
- **gRPC**: gRPC client for Go payment service
- **Plaid**: Plaid account integration
- **Crypto**: Crypto wallet integration

### Safety (`miriam_agent/safety/`)
- **Policy**: Safety and policy enforcement
- **Audit**: Audit and compliance system
- **Validator**: Input validation and sanitization

### Vector (`miriam_agent/vector/`)
- **pgVector**: Vector database implementation

### Conversational (`miriam_agent/conversational/`)
- **MemoryBased**: Memory-based conversational context

### Proactive (`miriam_agent/proactive/`)
- **Triggers**: Proactive triggers and alerts

## Configuration

### Environment Variables
- `DATABASE_URL`: PostgreSQL connection string
- `REDIS_URL`: Redis connection string
- `GRPC_ENDPOINT`: gRPC endpoint for payment service
- `PLAID_CLIENT_ID`: Plaid client ID
- `PLAID_SECRET`: Plaid secret
- `ENCRYPTION_KEY`: Encryption key for sensitive data
- `LOG_LEVEL`: Log level (DEBUG, INFO, WARNING, ERROR)
- `ENVIRONMENT`: Environment (development, production)

### Configuration Files
- `.env`: Environment variables
- `docker-compose.yml`: Docker configuration
- `docker-compose.dev.yml`: Development Docker configuration
- `otel-config.yaml`: OpenTelemetry configuration

## Dependencies

### Python Dependencies
- `fastapi>=0.104.0`
- `uvicorn>=0.24.0`
- `sqlalchemy>=2.0.23`
- `asyncpg>=0.29.0`
- `pgvector>=0.2.0`
- `redis>=5.0.1`
- `opentelemetry-api>=1.23.0`
- `opentelemetry-sdk>=1.23.0`
- `openai>=1.3.0`
- `httpx>=0.25.0`
- `cryptography>=41.0.0`
- `pyjwt>=2.8.0`
- `prometheus-client>=0.19.0`
- `structlog>=23.2.0`
- `python-multipart>=0.0.3`
- `orjson>=3.9.0`
- `click>=8.1.7`

### Development Dependencies
- `pytest>=7.4.3`
- `pytest-asyncio>=0.21.3`
- `pytest-cov>=4.1.0`
- `black>=23.12.0`
- `isort>=5.13.2`
- `mypy>=1.7.1`
- `ruff>=0.1.9`
- `pre-commit>=3.6.1`
- `factory-boy>=3.3.0`

## Testing

### Test Structure
```
tests/
├── test_auth.py                    # Authentication tests
├── test_core.py                     # Core functionality tests
├── test_agents.py                   # Agent tests
├── test_integrations.py             # Integration tests
├── test_safety.py                   # Safety tests
├── test_financial.py                # Financial intelligence tests
├── test_database.py                 # Database tests
└── conftest.py                      # Pytest fixtures
```

### Running Tests
```bash
# Run all tests
pytest

# Run specific test module
pytest tests/test_auth.py

# Run tests with coverage
pytest --cov=miriam_agent --cov-report=html

# Run tests with verbose output
pytest -v

# Run tests with custom markers
pytest -m "slow"
```

### Test Coverage
- Unit tests: Core functionality and modules
- Integration tests: API endpoints and integrations
- Security tests: Authentication and authorization
- Performance tests: Load and stress testing

## Development

### Setup
1. Clone the repository
2. Set up environment variables in `.env` file
3. Install dependencies:
   ```bash
   pip install -e .[dev]
   ```
4. Set up database:
   ```bash
   # For PostgreSQL with pgvector
   docker-compose up -d miriam-postgres
   ```
5. Initialize the application

### Running the Application
```bash
# Development mode
pip install -e .[dev]
uvicorn miriam_agent.cli:app --reload

# Production mode
uvicorn miriam_agent.cli:app
```

### Docker Configuration
```bash
# Run all services
docker-compose up -d

# Run with development overrides
docker-compose -f docker-compose.dev.yml up -d

# Stop and remove all services
docker-compose down
```

### Code Quality
```bash
# Format code
black .
isort .

# Check code style
black --check .
isort --check-only .
ruff check .
mypy miriam_agent

# Run pre-commit hooks
pre-commit run --all-files
```

## CI/CD

### GitHub Actions
The project includes GitHub Actions for CI/CD:

- **CI/CD Pipeline**: Tests and linting
- **Security Scan**: Security vulnerability scanning
- **Code Coverage**: Coverage reporting

### Configuration
`.github/workflows/ci.yml`

## Production Deployment

### Requirements
1. PostgreSQL with pgvector extension
2. Redis for caching and session storage
3. OpenTelemetry for observability
4. gRPC endpoint for payment service
5. Secure environment variables

### Deployment Steps
1. Set up infrastructure (Docker, Kubernetes, etc.)
2. Configure environment variables
3. Run database migrations
4. Deploy application
5. Configure monitoring and logging
6. Set up backups and disaster recovery

## Security

### Security Features
1. **Input Validation**: Comprehensive input validation and sanitization
2. **Authentication**: JWT-based authentication with role-based access control
3. **Authorization**: Fine-grained access control
4. **Encryption**: AES-256 encryption for sensitive data
5. **Audit Trails**: Immutable audit logs for all actions
6. **Rate Limiting**: Rate limiting for API endpoints
7. **Content Security Policy**: Protection against XSS and CSRF attacks
8. **Secure Headers**: Security headers for HTTP responses

### Security Controls
- All money movements require user approval
- Idempotency for all financial transactions
- Fail-safe behavior (fail closed)
- Encrypted data storage
- Secure API communication (HTTPS/gRPC)
- Regular security audits and vulnerability scanning

## Monitoring

### Observability
1. **Logging**: Structured logging with correlation IDs
2. **Metrics**: Prometheus metrics for monitoring
3. **Tracing**: OpenTelemetry distributed tracing
4. **Dashboard**: Grafana dashboard for system monitoring

### Alerting
1. **Proactive Alerts**: Proactive financial alerts
2. **System Alerts**: System health and performance alerts
3. **Security Alerts**: Security and compliance alerts

## Compliance

### Financial Compliance
1. **GDPR**: Personal data protection
2. **PCI DSS**: Payment card data security
3. **SOX**: Financial reporting compliance
4. **AML/KYC**: Anti-money laundering and know-your-customer

### Data Protection
1. **Data Retention**: Configurable data retention policies
2. **Data Encryption**: At-rest and in-transit encryption
3. **Access Control**: Role-based access control
4. **Audit Logging**: Comprehensive audit logs

## Performance

### Scalability
1. **Horizontal Scaling**: Scalable architecture for handling millions of users
2. **Caching**: Redis caching for frequently accessed data
3. **Load Balancing**: Load balancing for high availability
4. **Database Optimization**: Optimized database queries and indexing

### Performance Optimization
1. **Asynchronous Operations**: Non-blocking I/O operations
2. **Connection Pooling**: Database connection pooling
3. **Caching Strategies**: Multi-level caching
4. **Compression**: Data compression for storage and transmission

## Documentation

### API Documentation
- OpenAPI documentation available at `/docs` (development)
- Swagger UI at `/swagger-ui`
- ReDoc at `/redoc`

### Technical Documentation
- `README.md`: Project overview and setup instructions
- `README_FULL.md`: Complete technical documentation
- `CONTRIBUTION.md`: Contribution guidelines
- `LICENSE`: Project license

## Roadmap

### Phase 1: Foundation (Completed)
- Python service scaffolding
- Authentication and authorization
- Observability (logging, metrics, tracing)
- Database setup
- Vector memory layer
- Safety boundaries

### Phase 2: Agent Development (Completed)
- Agent base class and financial agent
- gRPC integration with Go payment service
- Database models and memory implementation
- Safety and policy enforcement

### Phase 3: Financial Intelligence (Completed)
- Financial intelligence module
- Budget planning and forecasting
- Transaction analysis
- Financial advice generation

### Phase 4: User Experience (Completed)
- Conversational layer
- Proactive features
- API endpoints

### Phase 5: Scale and Production (Upcoming)
- Horizontal scaling
- Advanced analytics
- Enhanced security
- Multi-tenancy support
