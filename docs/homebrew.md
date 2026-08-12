

# Homebrew Services

nyxGPT provides two persistent background services using **Homebrew services**:

1. **nyxgpt-api** - FastAPI backend (REST API)
2. **nyxgpt-web** - Next.js web UI

This is the recommended way to keep both services running locally without keeping terminals open.

---

## Prerequisites

- macOS
- Homebrew installed
- Python environment already set up for nyxGPT

---

## Homebrew tap

The nyxGPT Homebrew formula lives in a custom tap:

```
dkblinux98/nyxgpt-local
```

Add the tap:

```bash
brew tap dkblinux98/nyxgpt-local
```

`nyxgpt ops install` generates this tap's formulas **locally from a repo
checkout** (it vendors `pyproject.toml`/`src/nyxgpt/` and `web/` into a
tarball and points the formula at it via a `file://` URL) -- see
[Remote tap](#remote-tap) below for the repo-less alternative.

---

## Remote tap

For a machine that has never cloned nyxGPT, `nyxgpt ops install`'s local
`file://` tap above isn't an option -- there's no checkout to vendor a
tarball from. `.github/workflows/release-artifacts.yml` publishes a
**remote** tap instead (#3622): on every GitHub Release, it builds the same
`nyxgpt-api`/`nyxgpt-web` source tarballs, attaches them as release assets,
and pushes stamped formulas (real `url`/`sha256`/`version`, no placeholders)
to a separate tap repository the owner provisions once.

```bash
brew tap dkblinux98/homebrew-nyxgpt
brew install nyxgpt-api nyxgpt-web
```

Everything past `brew install` (services, wrappers, config resolution) is
identical to the local tap above -- the remote formulas
(`homebrew/tap/*.rb.tmpl`) are near-duplicates of the local ones
(`homebrew/nyxgpt-api.rb` / `nyxgpt-web.rb`), differing only in where the
tarball's `url` points.

**Owner setup (one-time, required before this tap exists):**

1. Create a public GitHub repo named `homebrew-nyxgpt` (the `homebrew-`
   prefix is Homebrew's tap-naming convention).
2. Add a repo variable `HOMEBREW_TAP_REPO` = `dkblinux98/homebrew-nyxgpt`
   and a secret `HOMEBREW_TAP_TOKEN` (a PAT with push access to that repo)
   to this repo's Actions settings.

Until both exist, `release-artifacts.yml`'s `homebrew-tap` job still builds
and attaches the tarballs to each GitHub Release and uploads the stamped
formulas as a workflow artifact -- it just skips the tap push (logged as a
notice, not a failure) until the owner completes the two steps above.

---

## Release-candidate formulas (rc channel)

Acceptance testing has to be able to install *unreleased* code on macOS the
same repo-less way a release installs -- otherwise the brew path can only
ever be accepted one release behind. Cutting a release candidate therefore
stamps the tap too (#3727):

```bash
brew tap dkblinux98/nyxgpt
brew install nyxgpt-api-rc nyxgpt-web-rc

brew services start nyxgpt-api-rc
brew services start nyxgpt-web-rc
```

Everything past `brew install` is identical to the stable formulas -- same
tarball contents, same self-contained Cellar keg, same wrappers, same
service names. `scripts/build_homebrew_artifacts.py --channel rc` derives
the `-rc` formulas from the same `homebrew/tap/*.rb.tmpl` templates, so the
two channels cannot drift about what the keg installs; only the class name,
the description and the conflict declaration differ.

### `brew install nyxgpt-api` is never affected

Homebrew has no pre-release semantics -- a tap serves whatever version its
formula names, and `brew install nyxgpt-api` takes it. So channel separation
lives in the **formula names**, not in a flag:

| | Stable | Release candidate |
| --- | --- | --- |
| Formulas | `nyxgpt-api`, `nyxgpt-web` | `nyxgpt-api-rc`, `nyxgpt-web-rc` |
| Written by | `release-artifacts.yml`, on a GitHub Release | `release-publish-pypi.yml`'s `homebrew-tap-rc` job, on an `rc` publish |
| Tarballs from | the release's GitHub Release | a GitHub **prerelease** for the RC (never "latest") |
| `brew install nyxgpt-api` resolves to | this | never this |

An `rc` publish never builds, copies or commits a stable formula file: the
job asserts none was produced, and the tap push refuses if a stable formula
would change. The nightly `dev` channel is PyPI-only and never touches the
tap at all.

### Why `-rc` and not `@rc`

Homebrew's `@` spelling is reserved for *versioned* formulas, and its loader
(`Formulary.class_s`) only translates `@` into `AT` when a **digit** follows
it: `python@3.12` becomes the class `PythonAT312`, but `nyxgpt-api@rc` would
become `NyxgptApi@rc` -- not a legal Ruby constant, so no class declaration
inside the file could satisfy the loader and `brew install nyxgpt-api@rc`
fails with `Expected to find class NyxgptApi@rc`. The candidate formulas are
therefore named `nyxgpt-api-rc`/`nyxgpt-web-rc`, which `brew` loads as
`NyxgptApiRc`/`NyxgptWebRc`. Every substantive property of the original
decision is unchanged -- separate formulas, stable never written,
`conflicts_with`, prerelease-only tarballs; only the spelling of the suffix
differs. `scripts/build_homebrew_artifacts.py` refuses to stamp any formula
name with an unloadable `@`, so the mistake cannot come back.

### Switching a machine between channels

The `-rc` formulas declare `conflicts_with` their stable counterparts,
because both install the same `nyxgpt-api`/`nyxgpt-web` wrappers and the
same brew service names. Switching channels is an explicit uninstall, never
a silent swap:

```bash
# stable -> release candidate
brew services stop nyxgpt-api && brew uninstall nyxgpt-api
brew install nyxgpt-api-rc && brew services start nyxgpt-api-rc

# ...and back once the release is out
brew services stop nyxgpt-api-rc && brew uninstall nyxgpt-api-rc
brew install nyxgpt-api && brew services start nyxgpt-api
```

`-rc` formulas are **acceptance-only**. They are not upgraded on a schedule,
carry no support expectation, and are superseded the moment the release they
are a candidate for ships. See
[docs/cloud.md](cloud.md#pypi-publishing-dev-rc-and-stable) for cutting one
and for the equivalent pip/cloud flows.

---

## Installing the services

Install both service formulas:

```bash
# Add the tap (if not already added)
brew tap dkblinux98/nyxgpt-local

# Install both services
brew install nyxgpt-api
brew install nyxgpt-web
```

Each service installs a self-contained app into its own Cellar keg --
`nyxgpt-api` gets its own Python venv (`pip install`ed from the vendored
source, not the repo checkout's editable `.venv`), and `nyxgpt-web` gets its
own `npm ci && npm run build` output. Neither depends on the repo checkout
existing or staying in place afterwards. Each install also gets:
- A small wrapper script
- A Homebrew launch agent plist

`nyxgpt ops install` only re-runs `brew install`/`reinstall` when the
vendored source actually changed since the last install (checksum-compared);
otherwise it reports the existing install is already up to date and just
(re)starts the service.

---

## Managing the API service (nyxgpt-api)

### Start the API

Start the FastAPI backend as a background service:

```bash
brew services start nyxgpt-api
```

Verify status:

```bash
brew services info nyxgpt-api
```

### Restart and stop

Restart the API service:

```bash
brew services restart nyxgpt-api
```

Stop the API service:

```bash
brew services stop nyxgpt-api
```

### API logs

The application's own structured logs (requests, self-heal, canary, RAG, ...) are written to:

```
~/.nyxGPT/logs/api.log
```

Tail logs in real time:

```bash
tail -f ~/.nyxGPT/logs/api.log
```

If the service fails to start (e.g. a crash before Python logging is even
configured), check Homebrew's own service log instead -- it captures the
process's raw stdout/stderr from the moment it launches:

```bash
tail -f "$(brew --prefix)/var/log/nyxgpt-api.log"
tail -f "$(brew --prefix)/var/log/nyxgpt-api.err.log"
```

`nyxgpt ops logs api` (and its `GET /api/v1/self-heal/logs?service=api`
API equivalent) already tails all three as labeled sections, so a
pre-logging startup failure -- e.g. the `[api] host`/`[auth] enabled` bind
refusal -- shows up there too, without needing the raw paths above (#3629).

---

## Managing the Web UI service (nyxgpt-web)

### Start the Web UI

Start the Next.js web UI as a background service:

```bash
brew services start nyxgpt-web
```

Verify status:

```bash
brew services info nyxgpt-web
```

The web UI will be available at: `http://127.0.0.1:3000`

### Restart and stop

Restart the web service:

```bash
brew services restart nyxgpt-web
```

Stop the web service:

```bash
brew services stop nyxgpt-web
```

### Web UI logs

The web UI has no structured `nyxgpt.logging`-based logging yet (tracked by
#3430), so Homebrew's own service log is the only place to look. Written to:

```
$(brew --prefix)/var/log/nyxgpt-web.log
$(brew --prefix)/var/log/nyxgpt-web.err.log
```

Tail logs in real time:

```bash
# Standard output logs
tail -f "$(brew --prefix)/var/log/nyxgpt-web.log"

# Error logs
tail -f "$(brew --prefix)/var/log/nyxgpt-web.err.log"
```

---

## Managing both services together

### Start both services

```bash
brew services start nyxgpt-api
brew services start nyxgpt-web
```

Or use the `nyxgpt ops restart` command for a coordinated restart:

```bash
nyxgpt ops restart
```

### Stop both services

```bash
brew services stop nyxgpt-api
brew services stop nyxgpt-web
```

### Check status of all services

```bash
brew services list | grep nyxgpt
```

Example output:

```
nyxgpt-api  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist
nyxgpt-web  started username ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web.plist
```

---

## Service dependencies

**Important:** The Web UI depends on the API service.

- **nyxgpt-api** must be running for the Web UI to function
- Start the API before starting the Web UI
- If the API is down, the Web UI will show connection errors

Recommended startup order:

```bash
brew services start nyxgpt-api
# Wait a few seconds for API to be ready
brew services start nyxgpt-web
```

---

## Configuration

Both Homebrew services use the same configuration file as the CLI:

```
~/.nyxGPT/config.ini
```

### API configuration

API settings are in the `[api]` section:

```ini
[api]
host = 127.0.0.1
port = 8000
```

### Web UI configuration

Web UI settings are in the `[web]` section:

```ini
[web]
host = 127.0.0.1
port = 3000
api_base_url =  # Optional: override API URL
```

### Applying configuration changes

Some settings are hot-reloadable (take effect immediately):
- `default_model`
- `rag.enable_chat_context`
- `logging.level`
- `auth.enabled` and `auth.api_key`

Other changes require service restart:

```bash
# Restart API service
brew services restart nyxgpt-api

# Restart Web UI service
brew services restart nyxgpt-web

# Restart both
nyxgpt ops restart
```

---

## Ollama model store

`nyxgpt ops install` points native Ollama's model store at
`~/.nyxGPT/volumes/ollama/models` -- the same directory
Compose/Terraform's `ollama` container uses -- instead of Ollama's own
default `~/.ollama/models`, via the `OLLAMA_MODELS` environment variable
(never a symlink). This means a model pulled while running in any one local
launch mode (native, `nyxgpt ops install --terraform --local`, or Compose)
shows up in all of them, with no duplicate downloads.

This is applied via `launchctl setenv OLLAMA_MODELS ...`, plus a
`com.nyxgpt.ollama-env` LaunchAgent that reapplies it at every login (a bare
`launchctl setenv` only lasts for the current session). If you ever pulled
models natively before upgrading, `nyxgpt ops install` merges anything found
in the old `~/.ollama/models` into the shared store automatically, the first
time it runs, without overwriting anything already there (via a hardlink
when possible, so multi-GB blobs merge instantly with no extra disk use).

Because `com.nyxgpt.ollama-env` and Homebrew's own `ollama` LaunchAgent are
both `RunAtLoad`, launchd does not guarantee which runs first on a given
login. So the env-setting script also force-restarts the `ollama` brew
service every time it runs, closing the race regardless of ordering: if
Homebrew's agent already started `ollama serve` with the wrong env, this
restarts it with the right one; if it hasn't started yet, this just starts
it. `nyxgpt ops doctor` also compares the live `launchctl getenv
OLLAMA_MODELS` against the expected shared path, in case something else
(e.g. a manual `launchctl unsetenv`) causes drift between logins.

Kubernetes is out of scope for this unification (its own PVC, no bind-mount
to the host's home directory possible) -- tracked separately.

---

## Accessing the services

After starting both services:

- **API**: `http://127.0.0.1:8000`
- **Web UI**: `http://127.0.0.1:3000`
- **API docs**: `http://127.0.0.1:8000/docs`

---

## Troubleshooting

### Web UI can't connect to API

**Symptom**: Web UI shows "Connection Error" or "API Unavailable"

**Solutions**:

1. Verify API is running:
   ```bash
   brew services list | grep nyxgpt-api
   curl http://127.0.0.1:8000/health
   ```

2. Check API logs:
   ```bash
   tail -f ~/.nyxGPT/logs/api.log
   ```

3. Restart API service:
   ```bash
   brew services restart nyxgpt-api
   ```

### Service won't start

**Symptom**: `brew services start` succeeds but service isn't running

**Solutions**:

1. Check logs for errors -- Homebrew's own service logs capture a crash
   before the process even reaches Python/Node logging setup, so check
   these first regardless of which service failed:
   ```bash
   tail -f "$(brew --prefix)/var/log/nyxgpt-api.err.log"
   tail -f "$(brew --prefix)/var/log/nyxgpt-web.err.log"
   ```

2. Verify configuration is valid:
   ```bash
   cat ~/.nyxGPT/config.ini
   ```

3. Check port conflicts:
   ```bash
   lsof -i :8000  # API port
   lsof -i :3000  # Web UI port
   ```

4. Run `nyxgpt ops doctor` for health checks:
   ```bash
   nyxgpt ops doctor
   ```

### Node.js not found (Web UI)

**Symptom**: Web UI logs show "node: command not found"

**Solutions**:

1. Verify Node.js is installed via Homebrew (the `nyxgpt-web` formula
   `depends_on "node"` and finds it via `Formula["node"]`, not `[paths]` in
   config.ini):
   ```bash
   brew list node
   ```

2. Reinstall the `node` formula if it's missing, then reinstall `nyxgpt-web`:
   ```bash
   brew install node
   nyxgpt ops install
   ```

3. Restart web service:
   ```bash
   brew services restart nyxgpt-web
   ```

---

## Notes

- Both services run under your user account (not as root)
- The API is bound to `127.0.0.1` by default and is not exposed publicly
- The Web UI is also bound to `127.0.0.1` for local-only access
- Homebrew services automatically restart both services on login
- Use `nyxgpt ops` commands for easier service management