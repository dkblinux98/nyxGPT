# myGPT Operations Guide

This document describes operational commands provided by `mygpt ops`. These commands manage local services and infrastructure required by myGPT, without requiring direct use of `brew`, `docker`, or `launchctl`.

All commands are safe to run multiple times and are designed for local, single-user systems.

---

## Overview

`mygpt ops` manages:

- FastAPI backend (`mygpt-api`)
- Local Web UI (`mygpt-web` / Next.js)
- Ollama (via Homebrew)
- Cassandra container (Docker)
- Cassandra log follower (LaunchAgent)

Configuration lives outside the repository in:

```
~/.myGPT/config.ini
```

Logs default to:

```
~/.myGPT/logs/
```

---

## Command Summary

```bash
mygpt ops install
mygpt ops status
mygpt ops restart
mygpt ops doctor
```

---

## `mygpt ops install`

Installs and registers all required local services.

This command:

- Installs Homebrew formulas (`mygpt-api`, `mygpt-web`) if missing
- Registers and loads required LaunchAgents
- Verifies Docker availability
- Ensures the Cassandra container exists
- Installs log-following helpers

Usage:

```bash
mygpt ops install
```

This command is idempotent. Existing services are not reinstalled unnecessarily.

---

## `mygpt ops status`

Displays the current runtime status of all managed components.

Usage:

```bash
mygpt ops status
```

Reports:

- Homebrew service state (`started`, `stopped`, `error`)
- Docker container state for Cassandra
- LaunchAgent load state

This command does not modify system state.

---

## `mygpt ops restart`

Gracefully restarts one or more myGPT-managed services.

This is the recommended way to:

- Apply configuration changes
- Recover from transient failures
- Restart services after updates

### Restart all components

```bash
mygpt ops restart
```

### Restart individual components

```bash
mygpt ops restart api
mygpt ops restart web
mygpt ops restart ollama
mygpt ops restart cassandra
mygpt ops restart cassandra-logs
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
mygpt ops doctor
```

---

## `mygpt ops doctor`

Runs a comprehensive system health check.

Usage:

```bash
mygpt ops doctor
```

Checks include:

- Required files under `~/.myGPT/`
- Homebrew availability
- Running services
- Docker daemon availability
- Cassandra container presence
- Log directory writability

Results are reported with clear PASS / FAIL indicators.

---

## Logs

All myGPT-managed services write logs under:

```
~/.myGPT/logs/
```

Typical files include:

- `mygpt-api.log`
- `mygpt-api.err.log`
- `mygpt-web.log`
- `mygpt-web.err.log`
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

- myGPT services managed exclusively through `mygpt ops`

This ensures services survive reboots and recover cleanly.

---

## Troubleshooting

If a service fails to start:

1. Run:
   ```bash
   mygpt ops status
   ```
2. Inspect logs in `~/.myGPT/logs/`
3. Run:
   ```bash
   mygpt ops doctor
   ```

Avoid manually invoking `brew services`, `docker run`, or `launchctl` unless explicitly debugging.

---

## Design Notes

- `mygpt ops` intentionally avoids destructive actions by default
- Data loss requires explicit user action
- All operations are local and user-scoped

```
