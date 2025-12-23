

# Homebrew Service

myGPT’s FastAPI backend can be run as a persistent background service using **Homebrew services**. This is the recommended way to keep the API running locally without keeping a terminal open.

---

## Prerequisites

- macOS
- Homebrew installed
- Python environment already set up for myGPT

---

## Homebrew tap

The myGPT Homebrew formula lives in a custom tap:

```
dkblinux98/mygpt-local
```

Add the tap:

```bash
brew tap dkblinux98/mygpt-local
```

---

## Installing the service

Install the FastAPI service formula:

```bash
brew install mygpt-api
```

This installs:

- a small wrapper script (`mygpt-api`)
- a Homebrew launch agent plist

---

## Starting the service

Start the API as a background service:

```bash
brew services start mygpt-api
```

Verify status:

```bash
brew services info mygpt-api
```

---

## Restarting and stopping

Restart the service:

```bash
brew services restart mygpt-api
```

Stop the service:

```bash
brew services stop mygpt-api
```

---

## Logs

Service logs are written to:

```
~/.myGPT/logs/mygpt.log
```

You can tail the logs in real time:

```bash
tail -f ~/.myGPT/logs/mygpt.log
```

If the service fails to start, this is the first place to check.

---

## Configuration

The Homebrew service uses the same configuration file as the CLI:

```
~/.myGPT/config.ini
```

Changes to configuration require a service restart:

```bash
brew services restart mygpt-api
```

---

## Notes

- The service runs under your user account (not as root).
- The API is bound to `127.0.0.1` by default and is not exposed publicly.
- Homebrew services automatically restart the API on login.