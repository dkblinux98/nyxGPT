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
- Session metadata is stored alongside it: `<session-name>.meta.json`

Examples:

```bash
mygpt chat                    # uses ~/.myGPT/sessions/default.json
mygpt chat --session work     # uses ~/.myGPT/sessions/work.json
mygpt chat --session work --new
```

### Sessions CLI

You can manage stored sessions directly from the command line:

```bash
mygpt sessions                         # list all sessions (shows title/summary/tags/pin)
mygpt sessions show NAME               # show full metadata for a session
mygpt sessions summarize NAME          # generate title/summary/tags using the model
mygpt sessions title NAME "New Title"  # set title manually
mygpt sessions pin NAME                # pin (pinned sessions sort first)
mygpt sessions unpin NAME              # unpin
mygpt sessions tag-add NAME tag1 tag2  # add tags
mygpt sessions tag-rm NAME tag1 tag2   # remove tags
mygpt sessions rename OLD NEW          # rename a session
mygpt sessions delete NAME             # delete a session (and its metadata)
```

Examples:

```bash
mygpt sessions
mygpt sessions summarize default
mygpt sessions pin default
mygpt sessions tag-add default chess training
mygpt sessions show default
mygpt sessions rename default brainstorming
mygpt sessions delete brainstorming
```

Because sessions live outside the repo:
- no `.gitignore` rules are required
- generated data is never committed by accident

## Notes

- Do **not** commit generated packaging metadata like `*.egg-info/`.
- Project name (distribution) is `myGPT`; the importable Python package is `mygpt`.