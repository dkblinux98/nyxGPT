# nyxGPT Deployment Checklist

This document provides a comprehensive pre-deployment checklist for ensuring production readiness of nyxGPT. Use this checklist before deploying to production environments.

---

## Table of Contents

1. [Security Configuration](#security-configuration)
2. [Performance Configuration](#performance-configuration)
3. [Monitoring Setup](#monitoring-setup)
4. [Backup Configuration](#backup-configuration)
5. [Health Check Verification](#health-check-verification)
6. [Pre-Deployment Validation](#pre-deployment-validation)

---

## Security Configuration

### Authentication and Authorization

- [ ] **API Key Authentication Enabled** (if exposed beyond localhost)
  ```ini
  [auth]
  enabled = true
  api_key = <strong-random-key>
  header = X-API-Key
  ```
  - Generate strong API key: `openssl rand -hex 32`
  - Store in secure secrets manager
  - Never commit API keys to version control

- [ ] **Rate Limiting Configured**
  ```ini
  [rate_limit]
  enabled = true
  requests_per_second = 10
  burst_size = 20
  ```
  - Adjust based on expected load
  - Monitor rate limit headers in production

- [ ] **CORS Configuration Reviewed**
  - Verify allowed origins in `src/nyxgpt/app.py`
  - Restrict to known domains only
  - Remove wildcard (`*`) origins in production

### Configuration File Security

- [ ] **Config File Permissions Locked Down**
  ```bash
  chmod 600 ~/.nyxGPT/config.ini
  ```
  - Verify ownership: `ls -la ~/.nyxGPT/config.ini`
  - Ensure not world-readable

- [ ] **Secrets Externalized**
  - [ ] No API keys in config files committed to git
  - [ ] GitHub PAT stored securely
  - [ ] OpenAI API key (if used) stored securely
  - [ ] Claude Code OAuth token stored securely

- [ ] **Session Directory Permissions**
  ```bash
  chmod 700 ~/.nyxGPT/sessions
  chmod 600 ~/.nyxGPT/sessions/*.json
  ```

### Network Security

- [ ] **API Bound to Correct Interface**
  ```ini
  [api]
  host = 127.0.0.1  # localhost only
  # OR
  host = 0.0.0.0    # all interfaces (requires auth + rate limiting)
  ```

- [ ] **Firewall Rules Configured** (if exposing externally)
  - Only expose necessary ports
  - Use reverse proxy (nginx/caddy) for HTTPS termination
  - Consider VPN or IP whitelist for admin access

- [ ] **HTTPS/TLS Configured** (if external access)
  - Use reverse proxy with valid TLS certificate
  - Enforce HTTPS redirects
  - HSTS headers enabled

### Data Security

- [ ] **Session Data Encryption at Rest** (if required by compliance)
  - Consider filesystem-level encryption
  - Document encryption strategy

- [ ] **Log Sanitization Verified**
  - No sensitive data (API keys, passwords) in logs
  - Review log output: `grep -i "key\|password\|token" ~/.nyxGPT/logs/*.log`

- [ ] **RAG Document Access Controls**
  - Verify who can upload documents
  - Implement document ownership/permissions if multi-user
  - Review ingested documents for sensitive content

---

## Performance Configuration

### Model Selection and Resource Allocation

- [ ] **Appropriate Model Chosen**
  ```ini
  [nyxgpt]
  default_model = qwen2.5:0.5b  # Fast, low memory
  # OR
  default_model = llama3.1:8b   # Balanced
  # OR
  default_model = llama3.1:70b  # High quality, requires GPU
  ```
  - Match model size to available hardware
  - Test model performance under load

- [ ] **Timeout Values Tuned**
  ```ini
  [nyxgpt]
  chat_timeout_seconds = 180

  [rag]
  embedding_timeout_seconds = 120
  reranker_timeout_seconds = 30
  ```
  - Test worst-case scenarios
  - Set timeouts to prevent runaway requests

- [ ] **Context Window Budgets Set**
  ```ini
  [context]
  default_window_size = 8192  # Adjust per model
  warning_threshold = 0.8
  ```
  - Verify model-specific overrides
  - Monitor context usage in production

### RAG Performance Tuning

- [ ] **RAG Parameters Optimized**
  ```ini
  [rag]
  chat_top_k = 5
  min_score = 0.0
  max_chunks = 6
  chat_context_max_chars = 2400
  chunk_size = 800
  chunk_overlap = 100
  embedding_batch_size = 16
  ```
  - Benchmark with realistic data
  - Balance quality vs speed

- [ ] **Hybrid Search Configured**
  ```ini
  [rag]
  enable_hybrid_search = true
  bm25_k1 = 1.5
  bm25_b = 0.75
  rrf_k = 60
  ```

- [ ] **Reranking Strategy Decided**
  ```ini
  [rag]
  enable_reranking = false  # Expensive, enable if quality critical
  rerank_top_n = 3
  ```

### Database and Cache Optimization

- [ ] **Cassandra Tuned for Production**
  - Memory limits appropriate: `docker stats nyxgpt-cassandra`
  - Compaction strategy verified
  - Connection pooling configured
  - Consider replication factor for HA

- [ ] **Session Storage Optimized**
  - Clean up old sessions periodically
  - Monitor session directory size: `du -sh ~/.nyxGPT/sessions/`
  - Consider archival strategy for old sessions

### Web UI Performance

- [ ] **Next.js Build Optimized**
  ```bash
  cd web && npm run build
  ```
  - Production build created
  - Static assets optimized
  - Consider CDN for assets if needed

---

## Monitoring Setup

### Health Checks

- [ ] **Health Check Endpoint Tested**
  ```bash
  curl http://127.0.0.1:8000/health
  ```
  - Returns 200 OK
  - Includes component status

- [ ] **System Health Monitoring Configured**
  ```bash
  nyxgpt ops doctor
  ```
  - Run periodically (cron job or monitoring agent)
  - Alert on failures

### Logging Configuration

- [ ] **Log Level Set Appropriately**
  ```ini
  [logging]
  level = INFO  # Production default
  # level = WARNING  # Low noise production
  # level = DEBUG  # Troubleshooting only
  ```

- [ ] **Log Directory Accessible**
  ```bash
  ls -la ~/.nyxGPT/logs/
  ```
  - Sufficient disk space
  - Log rotation configured
  - Backup/archive strategy defined

- [ ] **Centralized Logging** (if needed)
  - Forward logs to aggregation service
  - Structured logging format configured
  - Log retention policy defined

### Application Monitoring

- [ ] **Metrics Collection Configured**
  - API response times
  - Error rates
  - Request volume
  - RAG query performance
  - Model inference time

- [ ] **Alerting Rules Defined**
  - High error rate threshold
  - Service down alert
  - High latency alert
  - Disk space warnings
  - Memory/CPU alerts

- [ ] **Service Dependencies Monitored**
  - [ ] Ollama service status
  - [ ] Cassandra availability (if RAG enabled)
  - [ ] Docker daemon health
  - [ ] Disk I/O metrics

### Log Analysis and Debugging

- [ ] **Request ID Tracking Verified**
  - All requests have unique IDs
  - IDs propagate through call chain
  - Test with: `grep request_id ~/.nyxGPT/logs/*.log`

- [ ] **Error Tracking Integrated** (optional)
  - Sentry or similar error tracking
  - Error grouping and deduplication
  - Stack trace capture

---

## Backup Configuration

### Configuration Backup

- [ ] **Config File Backed Up**
  ```bash
  cp ~/.nyxGPT/config.ini ~/.nyxGPT/config.ini.backup
  ```
  - Store backup outside system
  - Document restore procedure
  - Regular backup schedule

- [ ] **Environment Variables Documented**
  - List all required environment variables
  - Document GitHub Actions variables
  - Store in secure password manager

### Data Backup

- [ ] **Session Data Backup Strategy**
  ```bash
  tar -czf sessions-backup-$(date +%Y%m%d).tar.gz ~/.nyxGPT/sessions/
  ```
  - Automated backup schedule
  - Offsite storage
  - Restore procedure tested

- [ ] **RAG Data Backup** (if using RAG)
  ```bash
  docker exec nyxgpt-cassandra nodetool snapshot nyxgpt
  ```
  - Cassandra snapshots configured
  - Snapshot retention policy
  - Restore procedure documented and tested

- [ ] **Document Backup** (if using RAG)
  - Original documents archived
  - Document metadata exported
  - Upload logs maintained

### Backup Verification

- [ ] **Restore Procedure Tested**
  - Test config restore
  - Test session restore
  - Test RAG data restore
  - Document restore time estimates

- [ ] **Backup Integrity Checks**
  - Verify backups are not corrupted
  - Test random sample restores
  - Automated backup verification

- [ ] **Disaster Recovery Plan**
  - RTO (Recovery Time Objective) defined
  - RPO (Recovery Point Objective) defined
  - Disaster recovery runbook created
  - DR drill scheduled

---

## Health Check Verification

### Pre-Deployment Health Checks

- [ ] **System Doctor Passes**
  ```bash
  nyxgpt ops doctor
  ```
  - All checks green
  - No warnings or errors

- [ ] **Service Status Verified**
  ```bash
  nyxgpt ops status
  ```
  - All services running
  - No failed services

### Component-Level Checks

- [ ] **Ollama Connectivity**
  ```bash
  curl http://127.0.0.1:11434/api/tags
  ```
  - Returns model list
  - Default model available

- [ ] **API Endpoints Functional**
  ```bash
  curl http://127.0.0.1:8000/health
  curl http://127.0.0.1:8000/api/v1/info
  ```
  - Health check passes
  - Info endpoint returns version

- [ ] **Web UI Accessible**
  ```bash
  curl http://127.0.0.1:3000
  ```
  - Returns 200 OK
  - UI loads in browser

- [ ] **Cassandra Healthy** (if RAG enabled)
  ```bash
  docker exec nyxgpt-cassandra nodetool status
  ```
  - Node status UP
  - No connection errors

### Functional Testing

- [ ] **Chat Flow End-to-End**
  ```bash
  nyxgpt chat "Hello, how are you?"
  ```
  - Response generated successfully
  - No errors in logs

- [ ] **Session Management Tested**
  ```bash
  nyxgpt sessions list
  nyxgpt sessions stats <session-name>
  ```
  - Sessions persist correctly
  - Metadata accurate

- [ ] **RAG Pipeline Tested** (if enabled)
  ```bash
  nyxgpt rag query "test query"
  ```
  - Results returned
  - Performance acceptable
  - No errors

- [ ] **API Rate Limiting Tested** (if enabled)
  - Rapid requests trigger rate limit
  - 429 responses returned correctly
  - Rate limit headers present

### Load Testing

- [ ] **Load Test Executed**
  - Simulate expected concurrent users
  - Measure response times under load
  - Identify performance bottlenecks
  - Verify no memory leaks

- [ ] **Stress Test Results Reviewed**
  - System behavior at 2x expected load
  - Graceful degradation verified
  - Recovery after stress verified

---

## Pre-Deployment Validation

### Code and Dependencies

- [ ] **All Tests Passing**
  ```bash
  pytest -v
  ```
  - Unit tests: 100% pass
  - Integration tests: 100% pass
  - No skipped critical tests

- [ ] **Type Checks Passing** (if using Python typing)
  ```bash
  mypy src/
  ```
  - No type errors

- [ ] **Linting Passing**
  ```bash
  ruff check src/ tests/
  ```
  - No linting errors

- [ ] **Dependencies Up to Date**
  ```bash
  pip list --outdated
  ```
  - Security patches applied
  - No known vulnerabilities: `pip-audit`

### Documentation

- [ ] **Deployment Documentation Complete**
  - [ ] Installation guide
  - [ ] Configuration guide
  - [ ] Operations runbook
  - [ ] Troubleshooting guide
  - [ ] Backup/restore procedures

- [ ] **Runbooks Created**
  - [ ] Standard operations (start, stop, restart)
  - [ ] Incident response procedures
  - [ ] Rollback procedures
  - [ ] Emergency contacts documented

### Operational Readiness

- [ ] **Access Controls Configured**
  - Admin access limited
  - SSH keys deployed
  - Service accounts created
  - Audit logging enabled

- [ ] **Deployment Automation Tested**
  - Deployment scripts tested
  - Rollback procedure verified
  - Zero-downtime deployment possible (if required)

- [ ] **Monitoring Dashboards Created**
  - System metrics dashboard
  - Application metrics dashboard
  - Error rate trends
  - Performance trends

- [ ] **On-Call Schedule Defined**
  - Primary on-call assigned
  - Secondary on-call assigned
  - Escalation path documented
  - Contact information current

### Compliance and Legal

- [ ] **Privacy Policy Reviewed** (if handling user data)
  - Data retention policy
  - Data deletion procedure
  - GDPR/CCPA compliance (if applicable)

- [ ] **Terms of Service Defined** (if multi-user)
  - Usage limits documented
  - Acceptable use policy
  - SLA defined (if applicable)

- [ ] **License Compliance Verified**
  - All dependencies have compatible licenses
  - License notices included
  - Open source obligations met

---

## Post-Deployment

### Immediate Post-Deployment

- [ ] **Smoke Tests Executed**
  - Core functionality verified
  - Critical paths tested
  - User acceptance testing completed

- [ ] **Monitoring Alerts Verified**
  - Alerts firing correctly
  - Alert routing working
  - On-call notified

- [ ] **Performance Baseline Established**
  - Response time metrics captured
  - Resource utilization recorded
  - Document as baseline for future comparison

### First 24 Hours

- [ ] **Continuous Monitoring**
  - Watch error rates
  - Monitor resource usage
  - Check log patterns
  - Verify backup jobs complete

- [ ] **User Feedback Collection**
  - Gather initial user feedback
  - Log any issues reported
  - Monitor support channels

### First Week

- [ ] **Performance Review**
  - Compare to baseline
  - Identify optimization opportunities
  - Adjust resource allocation if needed

- [ ] **Backup Verification**
  - Verify backups completing
  - Test restore on non-production
  - Adjust retention policies if needed

- [ ] **Security Audit**
  - Review access logs
  - Check for unusual patterns
  - Verify security controls effective

---

## Checklist Summary

This checklist covers the following critical areas:

1. **Security**: Authentication, authorization, secrets management, network security
2. **Performance**: Model selection, resource tuning, optimization
3. **Monitoring**: Health checks, logging, metrics, alerting
4. **Backup**: Configuration, data, disaster recovery
5. **Health Checks**: Pre-deployment validation, functional testing
6. **Validation**: Code quality, documentation, operational readiness

**Before deploying to production, ensure ALL items are checked and verified.**

For additional guidance, refer to:
- [Configuration Guide](configuration.md)
- [API Documentation](api.md)
- [Operations Guide](ops.md)
- [Troubleshooting Guide](troubleshooting.md)
- [Architecture Overview](architecture.md)

---

## Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2026-01-30 | 1.0 | Initial deployment checklist |
