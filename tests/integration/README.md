# Integration Tests

This directory contains integration tests that require external services to run.

## Requirements

Integration tests require the following services to be running:

### Required Services

1. **Ollama** (for LLM chat and streaming tests)
   - Must be running at `http://localhost:11434`
   - Required model: `llama3.1:8b` (or update test model in test files)

2. **Cassandra** (for RAG/vector store tests, if applicable)
   - Default connection: `localhost:9042`
   - Database: `mygpt`

## Setup

### Quick Start

Use the myGPT ops command to install and start all services:

```bash
# Install and start all required services
mygpt ops install

# Verify services are running
mygpt ops status
```

### Manual Setup

If you prefer to set up services manually:

#### Ollama

```bash
# Install Ollama (macOS)
brew install ollama

# Start Ollama service
ollama serve

# Pull required model
ollama pull llama3.1:8b
```

#### Cassandra (for RAG tests)

```bash
# Start Cassandra via Docker
docker run -d \
  --name mygpt-cassandra \
  -p 9042:9042 \
  cassandra:latest
```

## Running Integration Tests

### Run All Integration Tests

```bash
# Run all integration tests
pytest -m integration -v

# Run with coverage
pytest -m integration --cov=src/mygpt
```

### Run Specific Test Files

```bash
# Run only request ID streaming tests
pytest tests/integration/test_request_id_streaming.py -v

# Run only RAG integration tests
pytest tests/integration/test_rag_api_end_to_end.py -v
```

## Test Behavior

### Graceful Skipping

Integration tests will **automatically skip** if required services are not available:

```bash
$ pytest -m integration -v
...
test_request_id_propagates_in_streaming_response SKIPPED
  Reason: Ollama service not available at http://localhost:11434
```

This allows:
- ✅ Running unit tests without services (`pytest -m unit`)
- ✅ Running full test suite when services are available (`pytest`)
- ✅ CI/CD pipelines to run unit tests without infrastructure

### Service Availability Checks

Each test module checks for required services before running:
- **Ollama**: HTTP request to `http://localhost:11434/api/tags`
- **Cassandra**: Connection attempt to `localhost:9042`

## Troubleshooting

### Tests Skip Even Though Service is Running

1. Check service is accessible:
   ```bash
   curl http://localhost:11434/api/tags
   ```

2. Check model is installed:
   ```bash
   ollama list
   ```

3. Pull required model if missing:
   ```bash
   ollama pull llama3.1:8b
   ```

### Tests Hang or Timeout

- Check service health: `mygpt ops doctor`
- Restart services: `mygpt ops restart`
- Check logs in `~/.myGPT/logs/`

### Connection Refused

- Ensure service ports are not blocked by firewall
- Verify services are running: `mygpt ops status`
- Check Docker containers (for Cassandra): `docker ps`

## Configuration

### Using Different Models

To use a different Ollama model, update the test files:

```python
# In test file
json={
    "prompt": "Test prompt",
    "session": "test-session",
    "model": "your-model-name",  # Change this
}
```

### Using Different Ports

If running services on non-default ports, update configuration:

```bash
# Edit ~/.myGPT/config.ini
[ollama]
base_url = http://localhost:YOUR_PORT
```

## CI/CD Integration

For CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Run unit tests (no services required)
  run: pytest -m unit

- name: Start services (optional)
  run: docker-compose up -d

- name: Run integration tests (if services available)
  run: pytest -m integration
```

## Contributing

When adding new integration tests:

1. ✅ Mark with `@pytest.mark.integration`
2. ✅ Add service availability checks
3. ✅ Use `skipif` for graceful degradation
4. ✅ Document required services in this README
5. ✅ Use realistic test data (not production data)

## Additional Resources

- [Ollama Documentation](https://ollama.ai/docs)
- [Cassandra Documentation](https://cassandra.apache.org/doc/latest/)
