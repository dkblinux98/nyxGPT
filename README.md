# myGPT

A local, private "ChatGPT-style" project scaffold.

## Requirements

- Python (project currently uses a local `.venv`)

## Configuration

Runtime configuration is stored **outside** the repository:

- Real config: `~/.myGPT/config.ini`
- Template (checked in): `example.config.ini`

Create your local config:

```bash
mkdir -p ~/.myGPT
cp example.config.ini ~/.myGPT/config.ini
chmod 600 ~/.myGPT/config.ini
```

## Install (dev / editable)

From the repo root with your venv active:

```bash
pip install -e .
```

## Run

```bash
python -m mygpt
```

Or via the console script:

```bash
mygpt
```

## Sessions & Memory

Conversation history is stored **outside the repository** so that no generated data ever lives in the project tree.

- Sessions directory: `~/.myGPT/sessions/`
- Each session is stored as a JSON file: `<session-name>.json`

Examples:

```bash
mygpt chat                    # uses ~/.myGPT/sessions/default.json
mygpt chat --session work     # uses ~/.myGPT/sessions/work.json
mygpt chat --session work --new
```

Because sessions live outside the repo:
- no `.gitignore` rules are required
- generated data is never committed by accident

## Notes

- Do **not** commit generated packaging metadata like `*.egg-info/`.
- Project name (distribution) is `myGPT`; the importable Python package is `mygpt`.