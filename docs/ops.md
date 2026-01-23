# nyxGPT Operations Guide

This document describes operational commands provided by `nyxgpt ops`. These commands manage local services and infrastructure required by nyxGPT, without requiring direct use of `brew`, `docker`, or `launchctl`.

All commands are safe to run multiple times and are designed for local, single-user systems.

---

## Overview

`nyxgpt ops` manages:

- FastAPI backend (`nyxgpt-api`)
- Local Web UI (`nyxgpt-web` / Next.js)
- Ollama (via Homebrew)
- Cassandra container (Docker)
- Cassandra log follower (LaunchAgent)

Configuration lives outside the repository in:

```
~/.nyxGPT/config.ini
```

Logs default to:

```
~/.nyxGPT/logs/
```

---

## Command Summary

```bash
nyxgpt ops install
nyxgpt ops status
nyxgpt ops restart
nyxgpt ops doctor
```

---

## `nyxgpt ops install`

Installs and registers all required local services.

This command:

- Installs Homebrew formulas (`nyxgpt-api`, `nyxgpt-web`) if missing
- Registers and loads required LaunchAgents
- Verifies Docker availability
- Ensures the Cassandra container exists
- Installs log-following helpers

Usage:

```bash
nyxgpt ops install
```

This command is idempotent. Existing services are not reinstalled unnecessarily.

---

## `nyxgpt ops status`

Displays the current runtime status of all managed components.

Usage:

```bash
nyxgpt ops status
```

Reports:

- Homebrew service state (`started`, `stopped`, `error`)
- Docker container state for Cassandra
- LaunchAgent load state

This command does not modify system state.

---

## `nyxgpt ops restart`

Gracefully restarts one or more nyxGPT-managed services.

This is the recommended way to:

- Apply configuration changes
- Recover from transient failures
- Restart services after updates

### Restart all components

```bash
nyxgpt ops restart
```

### Restart individual components

```bash
nyxgpt ops restart api
nyxgpt ops restart web
nyxgpt ops restart ollama
nyxgpt ops restart cassandra
nyxgpt ops restart cassandra-logs
```

### Behavior

- Services are stopped and started cleanly
- Docker containers are **not recreated** unless missing
- Persistent volumes are preserved
- LaunchAgents are reloaded if installed

### Exit codes

- `0` — all requested services restarted successfully
- `2` — one or more services failed to restart

After restarting, it is recommended to run:

```bash
nyxgpt ops doctor
```

---

## `nyxgpt ops doctor`

Runs a comprehensive system health check.

Usage:

```bash
nyxgpt ops doctor
```

Checks include:

- Required files under `~/.nyxGPT/`
- Homebrew availability
- Running services
- Docker daemon availability
- Cassandra container presence
- Log directory writability

Results are reported with clear PASS / FAIL indicators.

---

## Logs

All nyxGPT-managed services write logs under:

```
~/.nyxGPT/logs/
```

Typical files include:

- `nyxgpt-api.log`
- `nyxgpt-api.err.log`
- `nyxgpt-web.log`
- `nyxgpt-web.err.log`
- `cassandra-logfollower.out.log`
- `cassandra-logfollower.err.log`

---

## Startup Behavior

Recommended configuration:

- Docker Desktop enabled at login
- Cassandra container started with:

```bash
--restart unless-stopped
```

- nyxGPT services managed exclusively through `nyxgpt ops`

This ensures services survive reboots and recover cleanly.

---

## Troubleshooting

If a service fails to start:

1. Run:
   ```bash
   nyxgpt ops status
   ```
2. Inspect logs in `~/.nyxGPT/logs/`
3. Run:
   ```bash
   nyxgpt ops doctor
   ```

Avoid manually invoking `brew services`, `docker run`, or `launchctl` unless explicitly debugging.

---

## Design Notes

- `nyxgpt ops` intentionally avoids destructive actions by default
- Data loss requires explicit user action
- All operations are local and user-scoped

```
