"""A filesystem path must not be named like a secret value (#3986).

`py/clear-text-logging-sensitive-data` classifies its sources by **name**, not
by what the value is. Any assignment whose target name carries a token like
`secret` is treated as a secret from then on, and every value derived from it
is tainted. `ops.py` hands the path of a Kubernetes Secret *manifest* to
`kubectl apply -f`, which is correct -- the manifest's VALUES must never reach
argv, and `_apply_k8s_secret_file`'s docstring says exactly that -- but a path
called `app_secret` tells CodeQL the filename is the secret. The path then
flows, legitimately, into the kubectl argv and from there into the subprocess
failure log, and the two logging calls in `_run`/`_log_nonzero_exit` are
reported as logging a secret in clear text.

That is what turned `CodeQL` red on this issue's own head: `f7008924` moved
`app_secret = K8S_DIR / "secret.yaml"` into the new `_k8s_wire_app_tier_dsn`,
which put the taint source inside the pull request's diff, and alerts 105,
106, 141 and 142 -- open on `v3.0.0` since August, and reported against
unchanged lines -- were attributed to the change. Nothing was newly logged;
only the attribution moved.

Measured rather than reasoned about, with the CodeQL CLI (2.27.1, the bundled
`python-queries` pack) on a four-variant reproduction:

| variant                                                  | flagged |
|----------------------------------------------------------|---------|
| local `app_secret`, callee parameter `secret_path`        | **yes** |
| local `app_tier_manifest`, callee parameter `secret_path` | no      |
| local `app_secret_d`, callee parameter `manifest_path`    | **yes** |
| local `app_tier_manifest`, callee parameter `manifest_path` | no    |

So the **caller's own name** is what classifies; a parameter named
`secret_path` is not a source. That is why this guard checks assignments and
leaves `_write_k8s_secret_value(secret_path, ...)` and
`_apply_k8s_secret_file(secret_path)` alone -- their parameter really is the
path of a Secret manifest, and renaming them would buy nothing.

**Out of scope on purpose, and named here so the next reader does not have to
re-derive it.** Two more sources reach the same two sinks, both module
constants that hold a Kubernetes *identifier* rather than a value:
`K8S_APP_SECRET_NAME`, which holds the Secret resource's own name, and
`K8S_ERROR_TRACKING_DSN_SECRET_KEY`, which holds a key inside it. Both are
already documented in `ops.py` as "key NAMES, not values", both must appear on a
`kubectl` command line for the command to mean anything, and both are open
alerts on `v3.0.0` today -- not something this change flips either way.
Renaming them would make the code read worse (they name a Secret object and a
key inside it) to satisfy a heuristic, and a *path* is a different case: it
has an accurate name that is not a secret word. If a future round does want
those two silenced, the honest route is a dismissal by the owner, not a
contorted constant.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "nyxgpt"

# The name tokens CodeQL's sensitive-data heuristic keys on. Deliberately the
# short list that matters for a path: a file called `secret.yaml` is the shape
# this repo actually has, and the rest are here so the next one is caught too.
SENSITIVE_NAME_TOKENS = (
    "secret",
    "password",
    "passwd",
    "credential",
    "token",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
)

# `Path`-shaped enough to be a filesystem path rather than arithmetic: a `/`
# join or an explicit `Path(...)`, with a literal filename somewhere in it.
_FILENAME = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def _looks_like_a_filename(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
        and bool(_FILENAME.search(sub.value) or "/" in sub.value)
        for sub in ast.walk(node)
    )


def _is_path_expression(node: ast.expr) -> bool:
    """Is this right-hand side a filesystem path, as opposed to a value?"""
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"Path", "PurePath", "joinpath", "with_name", "with_suffix", "expanduser"}:
            return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        # `K8S_DIR / "secret.yaml"` -- a join, not a division: the left end is
        # a name and a filename literal appears somewhere in it.
        leftmost: ast.AST = node
        while isinstance(leftmost, ast.BinOp):
            leftmost = leftmost.left
        if isinstance(leftmost, (ast.Name, ast.Attribute, ast.Call)) and _looks_like_a_filename(
            node
        ):
            return True
    return False


def _sensitive(name: str) -> str | None:
    lowered = name.lower()
    return next((token for token in SENSITIVE_NAME_TOKENS if token in lowered), None)


def _offending_assignments(path: Path) -> list[tuple[int, str, str]]:
    """(line, target name, matched token) for every path assigned a secret name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _is_path_expression(value):
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            token = _sensitive(target.id)
            if token is not None:
                found.append((node.lineno, target.id, token))
    return found


def test_no_filesystem_path_in_src_is_named_like_a_secret() -> None:
    offenders = [
        f"{path.relative_to(SRC.parents[1])}:{line} -- `{name}` holds a PATH and "
        f"carries the sensitive-name token `{token}`"
        for path in sorted(SRC.rglob("*.py"))
        for line, name, token in _offending_assignments(path)
    ]
    assert not offenders, (
        "A filesystem path named like a secret value becomes a CodeQL taint source "
        "(py/clear-text-logging-sensitive-data classifies by NAME), and these paths go "
        "on a kubectl command line, which puts them in the subprocess failure log. "
        "Name the path for what it is -- a manifest -- and the alert goes with it. "
        "See this module's docstring for the measurement.\n  " + "\n  ".join(offenders)
    )


def test_the_guard_detects_the_shape_it_exists_for(tmp_path: Path) -> None:
    """The pre-fix line is still caught; the post-fix one is not.

    Without this, a change to `_is_path_expression` that stops recognising a
    `/` join would make the guard above pass by seeing nothing at all.
    """
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "K8S_DIR = Path('/tmp/k8s')\n"
        "def before():\n"
        "    app_secret = K8S_DIR / 'secret.yaml'\n"
        "    return app_secret\n"
        "def after():\n"
        "    app_manifest = K8S_DIR / 'secret.yaml'\n"
        "    return app_manifest\n"
        "def arithmetic(total_secret_bits):\n"
        "    secret_strength = total_secret_bits / 8\n"
        "    return secret_strength\n",
        encoding="utf-8",
    )
    assert _offending_assignments(sample) == [(4, "app_secret", "secret")]
