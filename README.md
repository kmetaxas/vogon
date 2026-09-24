# Vogon

Vogon is the Django control plane for an AI-powered troubleshooting service. It orchestrates Marvin agents via gRPC, manages troubleshooting sessions with Temporal workflows, and exposes an MCP server for external tool integration.

## Overview

Vogon provides a centralized platform for running AI-assisted troubleshooting workflows across distributed infrastructure. It connects human operators with autonomous Marvin agents that execute capabilities (diagnostic tools, data collection, remediation actions) on target systems via a bi-directional gRPC stream.

### Key Concepts

- **Organization**: Multi-tenant boundary. All data is scoped to an organization.
- **Marvin**: An autonomous agent deployed on infrastructure that connects to Vogon via gRPC and advertises capabilities it can execute.
- **Capability**: A callable function exposed by a Marvin (e.g., `kubernetes.get_logs`, `network.ping`). Capabilities are parameterized via JSON Schema and searchable via semantic embeddings.
- **TSession (Troubleshooting Session)**: A Temporal-workflow-backed conversation between an operator and the AI assistant, composed of one or more threads.
- **Thread**: A conversation stream within a session. Supports visibility levels (public, private, shared).
- **ToolCall**: A dispatched capability execution targeting specific Marvins, with full lifecycle tracking (pending → in_progress → completed/failed).
- **Infrastructure Design**: Reusable topology documents (with Mermaid diagrams) describing your infrastructure, embeddable for LLM retrieval during troubleshooting.

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│   Browser   │◄────►│ Django +     │◄────►│  Temporal   │
│  (HTMX/WS)  │      │ DRF + WS     │      │ Workflows   │
└─────────────┘      └──────┬───────┘      └─────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   ┌────▼────┐        ┌────▼────┐        ┌────▼────┐
   │  gRPC   │        │   MCP   │        │PostgreSQL│
   │ Server  │        │ Server  │        │+ pgvector│
   └────┬────┘        └────┬────┘        └─────────┘
        │                  │
   ┌────▼──────────────────▼────┐
   │      Marvin Agents         │
   │  (on-prem / cloud / k8s)   │
   └────────────────────────────┘
```

## Getting Started

### Prerequisites

- Python 3.11+ / Django
- PostgreSQL 16+ (with pgvector extension)
- Temporal

### Installation

1. **Clone and install dependencies:**

   ```bash
   pip install -e ".[dev]"
   ```

2. **Set up environment variables** (copy `.env.example` to `.env` and fill in):

   ```bash
   DJANGO_SECRET_KEY=your-secret-key
   DJANGO_DEBUG=True
   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

   # Database
   DJANGO_DB_NAME=vogon
   DJANGO_DB_USER=postgres
   DJANGO_DB_PASSWORD=postgres
   DJANGO_DB_HOST=localhost
   DJANGO_DB_PORT=5432

   # Temporal
   TEMPORAL_HOST=localhost:7233
   TEMPORAL_NAMESPACE=default
   TEMPORAL_TASK_QUEUE=vogon

   # LLM (Ollama example)
   LLM_PROVIDER_TYPE=ollama
   LLM_BASE_URL=http://localhost:11434/v1
   LLM_MODEL=gpt-oss:20b

   # Embeddings (OpenAI example)
   EMBEDDING_PROVIDER_TYPE=openai
   EMBEDDING_API_KEY=sk-...
   EMBEDDING_MODEL=text-embedding-3-small

   # Google OAuth (optional)
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```

3. **Run database migrations:**

   ```bash
   python manage.py migrate
   ```

4. **Create a superuser:**

   ```bash
   python manage.py createsuperuser
   ```

### Running with Docker Compose

Start the full stack (PostgreSQL, Temporal, web, gRPC, MCP, Ollama):

```bash
docker compose up --build
```

Services will be available at:

- **Web UI**: <http://localhost:8000>
- **Temporal UI**: <http://localhost:8233>
- **gRPC**: localhost:50051
- **MCP**: <http://localhost:8080>
- **Ollama**: <http://localhost:11434>

### Running Locally (Development)

```bash
# Start PostgreSQL and Temporal
docker compose up postgres temporal temporal-ui

# Run migrations
python manage.py migrate

# Start the Django development server
python manage.py runserver

# In separate terminals:
python -m services.grpc_server.main    # gRPC server
python -m services.mcp_server.main     # MCP server
python -m services.temporal_workers.main  # Temporal workers
```

## Development

### Testing

```bash
pytest                              # Run all tests
pytest apps/core/tests.py          # Core app tests
pytest apps/sessions/tests.py -k test_send_message  # Specific test
```

Tests use `pytest-django` and mock Temporal workflows (no running server required).

## Environment Variables

| Variable                  | Default                  | Description             |
| ------------------------- | ------------------------ | ----------------------- |
| `DJANGO_SECRET_KEY`       | _(required)_             | Django secret key       |
| `DJANGO_DEBUG`            | `True`                   | Debug mode              |
| `DJANGO_ALLOWED_HOSTS`    | `localhost,127.0.0.1`    | Allowed hosts           |
| `TEMPORAL_HOST`           | `localhost:7233`         | Temporal server address |
| `TEMPORAL_NAMESPACE`      | `default`                | Temporal namespace      |
| `TEMPORAL_TASK_QUEUE`     | `vogon`                  | Temporal task queue     |
| `GRPC_PORT`               | `50051`                  | gRPC server port        |
| `MCP_PORT`                | `8080`                   | MCP server port         |
| `LLM_PROVIDER_TYPE`       | `ollama`                 | LLM provider type       |
| `LLM_BASE_URL`            | `https://ollama.com/v1`  | LLM API base URL        |
| `LLM_API_KEY`             | _(empty)_                | LLM API key             |
| `LLM_MODEL`               | `gpt-oss:20b`            | Model name              |
| `EMBEDDING_PROVIDER_TYPE` | `openai`                 | Embedding provider      |
| `EMBEDDING_API_KEY`       | _(empty)_                | Embedding API key       |
| `EMBEDDING_MODEL`         | `text-embedding-3-small` | Embedding model         |

## License

see LICENSE
