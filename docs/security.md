# Security Best Practices

This guide covers security best practices for deploying and operating nyxGPT in various environments. While nyxGPT is designed primarily for local, single-user development, these practices help secure your deployment when additional protection is needed.

---

## Table of Contents

1. [Default Security Posture](#default-security-posture)
2. [API Key Management](#api-key-management)
3. [Network Security](#network-security)
4. [Authentication Configuration](#authentication-configuration)
5. [Session Security](#session-security)
6. [Rate Limiting](#rate-limiting)
7. [Transport Security](#transport-security)
8. [Configuration Security](#configuration-security)
9. [Logging and Monitoring](#logging-and-monitoring)
10. [Security Checklist](#security-checklist)

---

## Default Security Posture

nyxGPT is designed as a **local-first, privacy-respecting** system with security-conscious defaults:

### Out-of-the-Box Security

- **Localhost-only binding**: API and web UI bind to `127.0.0.1` by default (not accessible from network)
- **No authentication required**: Authentication is disabled for local-only development
- **No rate limiting**: Rate limits are disabled for single-user localhost usage
- **No cloud dependencies**: All data stays on your machine
- **Explicit configuration**: No silent data exfiltration or external service calls

### Security Headers

All API responses include comprehensive security headers (automatically applied):

- **Content-Security-Policy**: Prevents XSS attacks by restricting resource loading
- **X-Content-Type-Options**: Prevents MIME sniffing attacks
- **X-Frame-Options**: Prevents clickjacking attacks
- **Strict-Transport-Security**: Enforces HTTPS (when using HTTPS)

See [docs/api.md#security-headers](api.md#security-headers) for full details.

---

## API Key Management

### When to Enable API Keys

Enable API key authentication when:

- The API is accessible from a network (even local network)
- Multiple users share the same machine
- You want defense-in-depth security
- You're exposing the API beyond localhost (not recommended)

### Generating Secure API Keys

**Always generate cryptographically secure random keys:**

```bash
# Generate a 32-byte random key (recommended)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Example output: `ZqJ9X_vK2nP8mR5tL3wH7yU4sN1aB6cE9fG0dI2jK8`

**Never use:**
- Predictable strings (e.g., "password", "secret", "apikey123")
- Short keys (less than 32 bytes)
- Keys from public examples or tutorials
- Keys shared across systems or environments

### Storing API Keys Securely

**Configuration file location:**
```
~/.nyxGPT/config.ini
```

**Set restrictive file permissions:**

```bash
# Only owner can read/write config file
chmod 600 ~/.nyxGPT/config.ini

# Verify permissions
ls -la ~/.nyxGPT/config.ini
# Expected: -rw------- (600)
```

**Never:**
- Commit `~/.nyxGPT/config.ini` to version control
- Share config files containing API keys
- Log or display API keys in application output
- Store API keys in environment variables visible to other processes
- Include API keys in URLs or query parameters

### Key Rotation

Rotate API keys periodically or immediately upon suspected compromise:

```bash
# 1. Generate new key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Update config
vim ~/.nyxGPT/config.ini
# [auth]
# api_key = <new-key-here>

# 3. Changes take effect immediately (hot-reload)
# No service restart needed!

# 4. Update all clients with new key
```

### API Key Configuration

Edit `~/.nyxGPT/config.ini`:

```ini
[auth]
# Enable API key authentication
enabled = true

# Shared secret (required when enabled)
api_key = <your-secure-key-here>

# HTTP header name for API key
# Default: X-API-Key
header = X-API-Key
```

**Using the API with authentication:**

```bash
curl http://127.0.0.1:8000/api/v1/info \
  -H "X-API-Key: your-secret-key-here"
```

See [docs/api.md#authentication](api.md#authentication) for complete API key documentation.

---

## Network Security

### Default Configuration (Recommended)

nyxGPT binds to localhost by default, making it inaccessible from the network:

```ini
[api]
host = 127.0.0.1
port = 8000

[web]
host = 127.0.0.1
port = 3000
```

**This is the recommended configuration for security.** Only localhost processes can access the API and web UI.

### Network Exposure (Not Recommended)

**Warning:** Exposing nyxGPT to a network introduces security risks. Only do this if you understand the implications and have implemented additional security measures.

If you must expose nyxGPT to a network:

```ini
[api]
# WARNING: Accessible from network
host = 0.0.0.0
port = 8000
```

**When binding to 0.0.0.0, you MUST:**

1. **Enable authentication:**
   ```ini
   [auth]
   enabled = true
   api_key = <strong-random-key>
   ```

2. **Enable rate limiting:**
   ```ini
   [rate_limit]
   enabled = true
   requests_per_second = 10
   burst_size = 20
   ```

3. **Use a firewall** to restrict access to trusted IP addresses:
   ```bash
   # Example: macOS firewall (pfctl)
   # Allow only specific IP
   sudo pfctl -e
   echo "pass in proto tcp from 192.168.1.0/24 to any port 8000" | sudo pfctl -f -
   ```

4. **Use HTTPS/TLS** with a reverse proxy (nginx, caddy) for encrypted transport

5. **Consider VPN or SSH tunneling** instead of direct network exposure:
   ```bash
   # SSH tunnel example (safer than network exposure)
   ssh -L 8000:127.0.0.1:8000 user@remote-host
   # Then access via http://127.0.0.1:8000 locally
   ```

### Firewall Configuration

**macOS (pfctl):**

```bash
# Allow localhost only (default)
# No additional rules needed

# If you must allow network access, restrict by IP:
sudo pfctl -e
echo "pass in proto tcp from 192.168.1.0/24 to any port 8000" | sudo pfctl -f -
```

**Linux (ufw):**

```bash
# Allow localhost only (default)
sudo ufw allow from 127.0.0.1 to any port 8000

# If you must allow network access, restrict by IP:
sudo ufw allow from 192.168.1.0/24 to any port 8000
```

**Linux (iptables):**

```bash
# Allow localhost only
sudo iptables -A INPUT -i lo -p tcp --dport 8000 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8000 -j DROP

# If you must allow network access, restrict by IP:
sudo iptables -A INPUT -s 192.168.1.0/24 -p tcp --dport 8000 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8000 -j DROP
```

### Network Security Checklist

- [ ] API binds to 127.0.0.1 (localhost only)
- [ ] Web UI binds to 127.0.0.1 (localhost only)
- [ ] If network exposure is required:
  - [ ] Authentication enabled with strong API key
  - [ ] Rate limiting enabled
  - [ ] Firewall rules restrict access to trusted IPs
  - [ ] HTTPS/TLS configured via reverse proxy
  - [ ] Monitoring and alerting configured
- [ ] Consider SSH tunneling instead of direct network exposure

---

## Authentication Configuration

### Enabling Authentication

Edit `~/.nyxGPT/config.ini`:

```ini
[auth]
enabled = true
api_key = <generate-strong-key-here>
header = X-API-Key
```

**Generate a strong key:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Hot-Reload Support

Authentication configuration is reloaded on every request. Changes take effect immediately without restarting services:

```bash
# 1. Edit config
vim ~/.nyxGPT/config.ini

# 2. Save changes
# [auth]
# enabled = true
# api_key = new-key

# 3. Next request requires authentication (no restart!)
```

### Security Features

**Constant-time comparison:**
- API keys are compared using `secrets.compare_digest()` to prevent timing attacks
- Attackers cannot determine the correct key by measuring response times

**Request ID tracking:**
- All authentication failures include a unique request ID
- Enables correlation of failed attempts across logs
- Useful for security auditing and incident response

**Example error response:**
```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or missing API key",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

**Check logs for details:**
```bash
grep "550e8400-e29b-41d4-a716-446655440000" ~/.nyxGPT/logs/nyxgpt.log
```

### Exempt Endpoints

These endpoints remain accessible without authentication:

- `/health` - Health check
- `/docs` - API documentation
- `/openapi.json` - OpenAPI schema
- `/redoc` - ReDoc documentation

This ensures monitoring and documentation remain accessible while protecting functional endpoints.

### Web UI Integration

The Next.js web UI automatically reads `~/.nyxGPT/config.ini` and includes the API key in backend requests. No additional configuration needed.

---

## Session Security

### Session Storage

Session files are stored outside the repository:

```
~/.nyxGPT/sessions/
```

**File permissions:**

```bash
# Set restrictive permissions on session directory
chmod 700 ~/.nyxGPT/sessions

# Verify
ls -ld ~/.nyxGPT/sessions
# Expected: drwx------ (700)
```

### Session Data Protection

Sessions may contain sensitive information (prompts, responses, RAG document citations). Protect session files:

**Best practices:**

1. **Restrict file permissions:**
   ```bash
   chmod 700 ~/.nyxGPT/sessions
   chmod 600 ~/.nyxGPT/sessions/*.json
   ```

2. **Exclude from backups (if needed):**
   ```bash
   # macOS Time Machine exclusion
   tmutil addexclusion ~/.nyxGPT/sessions

   # Verify
   tmutil isexcluded ~/.nyxGPT/sessions
   ```

3. **Use encrypted filesystems** for sensitive data:
   - macOS: Enable FileVault
   - Linux: Use LUKS/dm-crypt
   - Encrypted home directories

4. **Clean up old sessions periodically:**
   ```bash
   # List old sessions (older than 90 days)
   find ~/.nyxGPT/sessions -name "*.json" -mtime +90

   # Delete old sessions
   find ~/.nyxGPT/sessions -name "*.json" -mtime +90 -delete
   ```

### Session Naming and Privacy

Auto-generated session titles may contain sensitive information. Review session names:

```bash
# List all session titles
nyxgpt sessions list

# Rename sensitive sessions
nyxgpt sessions rename old-name new-name
```

**Configure auto-summarization:**

```ini
[nyxgpt]
# Disable auto-summarization if privacy is a concern
auto_summarize_enabled = false

# Or increase threshold to summarize less frequently
auto_summarize_after_messages = 50
```

### RAG Document Security

RAG documents are ingested into Cassandra. Protect sensitive documents:

1. **Review uploaded documents:**
   ```bash
   curl http://127.0.0.1:8000/api/v1/rag/documents
   ```

2. **Use document metadata filters** to limit exposure:
   ```bash
   # Only search specific documents
   curl -X POST http://127.0.0.1:8000/api/v1/rag/query \
     -H "Content-Type: application/json" \
     -d '{"query": "test", "doc_ids": ["public-doc-1", "public-doc-2"]}'
   ```

3. **Clear collections periodically:**
   ```bash
   # Clear non-default collections
   curl -X DELETE http://127.0.0.1:8000/api/v1/rag/collections/temp-collection
   ```

4. **Protect Cassandra data:**
   ```bash
   # Cassandra data volume
   docker volume inspect nyxgpt_cassandra_data
   ```

---

## Rate Limiting

### When to Enable Rate Limiting

Enable rate limiting when:

- The API is accessible from a network
- You want protection against DoS attacks
- Multiple users share the API
- You're exposing the API beyond localhost

### Enabling Rate Limiting

Edit `~/.nyxGPT/config.ini`:

```ini
[rate_limit]
# Enable rate limiting
enabled = true

# Maximum sustained requests per second per IP
requests_per_second = 10

# Maximum burst size (tokens in bucket)
burst_size = 20
```

### How Rate Limiting Works

nyxGPT uses a **token bucket algorithm**:

1. Each IP address gets a bucket with `burst_size` tokens
2. Tokens are added at `requests_per_second` rate
3. Each request consumes 1 token
4. Requests are denied when bucket is empty

**Example with defaults:**
- Initial burst: 20 requests immediately
- Sustained rate: 10 requests/second
- After burst, client must wait for token refill

### Rate Limit Headers

All responses include rate limit headers:

```
X-RateLimit-Limit: 20
X-RateLimit-Remaining: 15
X-RateLimit-Reset: 1704067200
```

### Rate Limit Error Response

When limit is exceeded (HTTP 429):

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Too many requests. Please try again later.",
    "request_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

### Tuning Rate Limits

**For light usage:**
```ini
[rate_limit]
requests_per_second = 5
burst_size = 10
```

**For heavy usage:**
```ini
[rate_limit]
requests_per_second = 20
burst_size = 50
```

**For testing:**
```ini
[rate_limit]
requests_per_second = 100
burst_size = 200
```

### Rate Limiting Best Practices

- **Monitor rate limit metrics** via logs and headers
- **Set alerts** for repeated rate limit violations
- **Adjust limits** based on actual usage patterns
- **Consider per-endpoint limits** for expensive operations (custom implementation required)
- **Whitelist trusted IPs** if needed (custom implementation required)

---

## Transport Security

### Local Development (HTTP)

nyxGPT uses HTTP by default for localhost-only development:

```
http://127.0.0.1:8000
http://127.0.0.1:3000
```

**This is secure for localhost** because:
- Traffic never leaves your machine
- No network exposure
- No man-in-the-middle risk

### Network Exposure (HTTPS Required)

**If you expose nyxGPT to a network, you MUST use HTTPS.**

HTTP over a network exposes:
- API keys in plaintext
- Session data in plaintext
- RAG documents in plaintext
- Chat prompts and responses in plaintext

### Setting Up HTTPS with Reverse Proxy

**Recommended approach:** Use nginx or caddy as a reverse proxy with automatic TLS.

#### Option 1: Caddy (Easiest)

Caddy automatically provisions TLS certificates from Let's Encrypt.

**Install Caddy:**
```bash
# macOS
brew install caddy

# Ubuntu/Debian
sudo apt install caddy
```

**Caddyfile example:**
```
nyxgpt.example.com {
    reverse_proxy localhost:8000
}

nyxgpt-web.example.com {
    reverse_proxy localhost:3000
}
```

**Start Caddy:**
```bash
sudo caddy run --config Caddyfile
```

Caddy will automatically:
- Provision TLS certificates
- Handle certificate renewal
- Redirect HTTP to HTTPS
- Serve HTTPS on port 443

#### Option 2: nginx

**Install nginx:**
```bash
# macOS
brew install nginx

# Ubuntu/Debian
sudo apt install nginx
```

**Generate self-signed certificate (testing only):**
```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/nyxgpt.key \
  -out /etc/ssl/certs/nyxgpt.crt
```

**nginx configuration:**
```nginx
server {
    listen 443 ssl;
    server_name nyxgpt.example.com;

    ssl_certificate /etc/ssl/certs/nyxgpt.crt;
    ssl_certificate_key /etc/ssl/private/nyxgpt.key;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Start nginx:**
```bash
sudo nginx -t  # Test config
sudo nginx -s reload
```

### SSH Tunneling (Best Alternative)

**Recommended:** Use SSH tunneling instead of exposing nyxGPT directly to a network.

**From remote machine:**
```bash
# Forward remote port 8000 to local port 8000
ssh -L 8000:127.0.0.1:8000 user@nyxgpt-host

# Then access API locally
curl http://127.0.0.1:8000/api/v1/info
```

**Benefits:**
- Encrypted transport via SSH
- No additional TLS configuration needed
- No need to expose ports to network
- Uses existing SSH authentication

---

## Configuration Security

### Configuration File Protection

**File location:**
```
~/.nyxGPT/config.ini
```

**Set restrictive permissions:**
```bash
chmod 600 ~/.nyxGPT/config.ini
```

**Verify permissions:**
```bash
ls -la ~/.nyxGPT/config.ini
# Expected: -rw------- (600)
```

### Secrets in Configuration

The config file may contain sensitive information:

- API keys (`[auth] api_key`)
- OpenAI API keys (`[openai] api_key`)
- GitHub tokens (`[github] pat`)
- Database credentials (if configured)

**Best practices:**

1. **Never commit config files to version control**
2. **Use environment-specific configs** (dev, staging, prod)
3. **Audit config changes** periodically
4. **Rotate secrets** when team members leave
5. **Use secrets management tools** for production (e.g., HashiCorp Vault, AWS Secrets Manager)

### Example Configuration (Secure)

```ini
[auth]
enabled = true
api_key = <use-python-secrets-to-generate>
header = X-API-Key

[rate_limit]
enabled = true
requests_per_second = 10
burst_size = 20

[api]
host = 127.0.0.1  # Localhost only
port = 8000

[logging]
level = INFO  # Don't use DEBUG in production (may log sensitive data)
```

### Configuration Backup

**Backup securely:**
```bash
# Create encrypted backup
tar czf - ~/.nyxGPT/config.ini | \
  openssl enc -aes-256-cbc -salt -out config-backup.tar.gz.enc

# Restore
openssl enc -d -aes-256-cbc -in config-backup.tar.gz.enc | \
  tar xzf - -C ~/
```

---

## Logging and Monitoring

### Log Locations

```
~/.nyxGPT/logs/
├── nyxgpt.log              # Application logs
├── nyxgpt-api.log          # API service logs
├── nyxgpt-web.log          # Web UI logs
├── cassandra-logfollower.out.log
└── ollama.log              # Symlinked from Homebrew
```

### Security-Relevant Log Events

Monitor logs for:

- **Authentication failures**: `grep "unauthorized" ~/.nyxGPT/logs/nyxgpt.log`
- **Rate limit violations**: `grep "rate_limit_exceeded" ~/.nyxGPT/logs/nyxgpt.log`
- **Unusual API usage**: `grep "api/v1" ~/.nyxGPT/logs/nyxgpt.log`
- **Session access patterns**: `grep "session" ~/.nyxGPT/logs/nyxgpt.log`

### Log File Security

**Set restrictive permissions:**
```bash
chmod 700 ~/.nyxGPT/logs
chmod 600 ~/.nyxGPT/logs/*.log
```

**Rotate logs periodically:**
```bash
# Example: Keep logs for 30 days
find ~/.nyxGPT/logs -name "*.log" -mtime +30 -delete
```

### Monitoring Authentication Failures

**Track failed authentication attempts:**
```bash
# Count failures per IP
grep "unauthorized" ~/.nyxGPT/logs/nyxgpt.log | \
  grep -oP 'X-Forwarded-For: \K[^,]+' | \
  sort | uniq -c | sort -nr
```

**Set up alerts:**
```bash
# Example: Alert on 10+ failures in 5 minutes
#!/bin/bash
FAILURES=$(grep "unauthorized" ~/.nyxGPT/logs/nyxgpt.log | \
  tail -n 100 | wc -l)

if [ "$FAILURES" -gt 10 ]; then
  echo "WARNING: $FAILURES authentication failures detected" | \
    mail -s "nyxGPT Security Alert" admin@example.com
fi
```

### What NOT to Log

**Never log sensitive data:**
- API keys or tokens
- Full request bodies containing passwords
- Session data or chat prompts (use request IDs for correlation)
- RAG document content
- User credentials

**Current log level configuration:**
```ini
[logging]
level = INFO  # Recommended for production
# level = DEBUG  # Only for development (may log sensitive data)
```

---

## Security Checklist

### Local Development (Minimal Security)

- [ ] API binds to 127.0.0.1
- [ ] Web UI binds to 127.0.0.1
- [ ] Config file has 600 permissions
- [ ] Session directory has 700 permissions
- [ ] Authentication disabled (default)
- [ ] Rate limiting disabled (default)

### Shared Machine (Moderate Security)

- [ ] All items from "Local Development"
- [ ] Authentication enabled with strong API key
- [ ] API key generated using `secrets.token_urlsafe(32)`
- [ ] Config file permissions verified (600)
- [ ] Session files permissions verified (600)
- [ ] Logs reviewed periodically for unusual activity

### Network Exposure (High Security) - Not Recommended

**Warning:** Network exposure introduces significant security risks. Only proceed if necessary and with full understanding.

- [ ] All items from "Shared Machine"
- [ ] Rate limiting enabled
- [ ] HTTPS configured via reverse proxy (nginx/caddy)
- [ ] Firewall rules restrict access to trusted IPs
- [ ] Strong API key (32+ bytes)
- [ ] API key rotation schedule established
- [ ] Monitoring and alerting configured
- [ ] Log retention and analysis in place
- [ ] Consider SSH tunneling instead

### Production Deployment (Not Officially Supported)

nyxGPT is designed for local, single-user use. For production:

- [ ] All items from "Network Exposure"
- [ ] Per-user authentication (not shared API keys)
- [ ] OAuth2 or JWT implementation
- [ ] Database-backed session management
- [ ] Comprehensive audit logging
- [ ] DDoS protection
- [ ] Security incident response plan
- [ ] Regular security audits
- [ ] Penetration testing

**Note:** Production deployment is beyond the scope of nyxGPT's design. Consider using enterprise-ready alternatives or adding significant infrastructure tooling.

---

## Security Limitations

### What nyxGPT Security Does NOT Provide

API key authentication and rate limiting are **basic security controls**. They do NOT provide:

- **Network security**: Use firewalls, VPNs, or SSH tunnels
- **Transport encryption**: Use HTTPS/TLS via reverse proxy
- **Per-user authentication**: nyxGPT uses shared API keys, not per-user accounts
- **Session isolation**: All API key holders have access to all sessions
- **Input sanitization beyond basic validation**: Application-level validation is enforced but may not catch all edge cases
- **Protection against compromised localhost**: If an attacker has local access, they can read config files and bypass authentication

### Known Limitations

1. **Shared API key**: All clients with the key have full access
2. **No user accounts**: Cannot restrict access per user
3. **No audit trail**: Limited visibility into who accessed what
4. **No session isolation**: All authenticated clients can access all sessions
5. **No secrets management**: API keys stored in plaintext config file (use filesystem encryption)

---

## Getting Help

For security issues:

1. **Check documentation**: [docs/api.md](api.md), [docs/troubleshooting.md](troubleshooting.md)
2. **Review logs**: `~/.nyxGPT/logs/nyxgpt.log`
3. **Report security vulnerabilities**: https://github.com/anthropics/nyxGPT/security
4. **General questions**: https://github.com/anthropics/nyxGPT/issues

**Do not publicly disclose security vulnerabilities.** Use GitHub's private security reporting feature.

---

## Summary

nyxGPT's security model prioritizes:

1. **Local-first by default** (no network exposure)
2. **Explicit configuration** (no silent defaults)
3. **Defense in depth** (multiple layers when needed)
4. **Privacy-respecting** (no external dependencies)
5. **Audit trail** (request IDs, logging)

**Key takeaways:**

- **Default configuration is secure for local development**
- **Enable authentication and rate limiting for shared machines**
- **Use HTTPS and firewalls for network exposure (not recommended)**
- **Prefer SSH tunneling over direct network exposure**
- **Protect config files and session data with filesystem permissions**
- **Monitor logs for security events**
- **Rotate API keys periodically**
- **Report security issues privately**

For most users, the default configuration (localhost-only, no authentication) is sufficient and secure.
