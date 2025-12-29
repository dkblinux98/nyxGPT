from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tomllib


# Repo root: .../myGPT/src/mygpt/ops.py -> parents[2] is repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OpsResult:
    ok: bool
    message: str
    details: str = ""


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _which(prog: str) -> Optional[str]:
    return shutil.which(prog)


def _read_project_version() -> str:
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "1.0.0"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data.get("project", {}).get("version", "1.0.0"))


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _copy_file(src: Path, dst: Path, *, mode: Optional[int] = None) -> None:
    _ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    if mode is not None:
        os.chmod(dst, mode)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _brew_prefix() -> Path:
    try:
        cp = _run(["brew", "--prefix"])
        return Path((cp.stdout or "").strip())
    except Exception:
        return Path("/usr/local")


def _tap_repo(tap: str) -> Path:
    cp = _run(["brew", "--repo", tap])
    return Path((cp.stdout or "").strip())


def _find_launchagent_template() -> tuple[Optional[Path], list[Path]]:
    """
    Locate the Cassandra log follower LaunchAgent template inside the repo.
    Returns (path_or_none, candidates_checked).
    """
    candidates = [
        REPO_ROOT / "ops" / "launchagents" / "com.mygpt.cassandra-logs.plist",
        REPO_ROOT / "ops" / "LaunchAgents" / "com.mygpt.cassandra-logs.plist",
        REPO_ROOT / "com.mygpt.cassandra-logs.plist",
        REPO_ROOT / "homebrew" / "com.mygpt.cassandra-logs.plist",
    ]
    for p in candidates:
        try:
            if p.exists():
                return p, candidates
        except Exception:
            # If something odd happens (permissions, broken symlink), keep searching.
            continue
    return None, candidates


def _install_scripts() -> list[OpsResult]:
    results: list[OpsResult] = []
    src_dir = REPO_ROOT / "scripts"
    dst_dir = Path.home() / ".myGPT" / "scripts"
    _ensure_dir(dst_dir)

    for name in ("run-web.sh", "follow-cassandra-logs.sh"):
        src = src_dir / name
        if not src.exists():
            # Not required — some users run the web/API without wrappers.
            results.append(OpsResult(True, f"Script not present (skipped): {name}", str(src)))
            continue
        dst = dst_dir / name
        _copy_file(src, dst, mode=0o755)
        results.append(OpsResult(True, f"Installed script {name}", str(dst)))

    return results


def _install_cassandra_launchagent() -> list[OpsResult]:
    results: list[OpsResult] = []
    tpl, checked = _find_launchagent_template()
    if tpl is None:
        details = "Tried:\n" + "\n".join(str(p) for p in checked) + f"\nREPO_ROOT={REPO_ROOT}"
        results.append(OpsResult(False, "Missing Cassandra logs LaunchAgent template", details))
        return results
    la_dir = Path.home() / "Library" / "LaunchAgents"
    _ensure_dir(la_dir)
    dst = la_dir / tpl.name
    _copy_file(tpl, dst)

    label = "com.mygpt.cassandra-logs"
    domain = f"gui/{os.getuid()}"

    _run(["launchctl", "bootout", domain, str(dst)], check=False)
    _run(["launchctl", "bootstrap", domain, str(dst)], check=False)
    _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"], check=False)

    results.append(OpsResult(True, "Installed Cassandra logs LaunchAgent", str(dst)))
    return results


def _ensure_log_symlinks() -> list[OpsResult]:
    results: list[OpsResult] = []
    home_logs = Path.home() / ".myGPT" / "logs"
    _ensure_dir(home_logs)

    brew_logs = _brew_prefix() / "var" / "log"
    for base in ("mygpt-api", "mygpt-web"):
        for ext in (".log", ".err.log"):
            src = brew_logs / f"{base}{ext}"
            dst = home_logs / f"{base}{ext}"
            try:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(src)
                results.append(OpsResult(True, f"Symlinked {dst.name}", f"{dst} -> {src}"))
            except Exception as e:
                results.append(OpsResult(False, f"Failed to symlink {dst.name}", str(e)))
    return results


def _create_dist_tarball(tap_dir: Path, name: str, version: str) -> Path:
    dist_dir = tap_dir / "dist"
    _ensure_dir(dist_dir)
    tar_path = dist_dir / f"{name}-{version}.tar.gz"

    tmp = dist_dir / f".tmp-{name}-{version}"
    if tmp.exists():
        shutil.rmtree(tmp)
    _ensure_dir(tmp)
    (tmp / "README.txt").write_text(
        f"{name} {version}\nGenerated by mygpt ops install\n",
        encoding="utf-8",
    )

    if tar_path.exists():
        tar_path.unlink()

    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(tmp, arcname=f"{name}-{version}")

    shutil.rmtree(tmp, ignore_errors=True)
    return tar_path


def _install_homebrew_web(tap: str = "dkblinux98/mygpt-local") -> list[OpsResult]:
    results: list[OpsResult] = []
    if _which("brew") is None:
        return [OpsResult(False, "Homebrew not found", "")]

    template = REPO_ROOT / "homebrew" / "mygpt-web.rb"
    if not template.exists():
        return [OpsResult(False, "Missing homebrew/mygpt-web.rb", str(template))]

    tap_dir = _tap_repo(tap)
    version = _read_project_version()

    tar = _create_dist_tarball(tap_dir, "mygpt-web", version)
    sha = _sha256_file(tar)
    url = f"file://{tar}"

    content = template.read_text(encoding="utf-8")
    content = content.replace("__MYGPT_WEB_URL__", url)
    content = content.replace("__MYGPT_WEB_SHA256__", sha)

    formula_dir = tap_dir / "Formula"
    _ensure_dir(formula_dir)
    dst = formula_dir / "mygpt-web.rb"
    dst.write_text(content, encoding="utf-8")
    results.append(OpsResult(True, "Installed mygpt-web formula", str(dst)))

    _run(["brew", "install", "--overwrite", f"{tap}/mygpt-web"], check=False)
    _run(["brew", "services", "start", "mygpt-web"], check=False)
    results.append(OpsResult(True, "Requested brew install/start mygpt-web", ""))

    return results


def install(args) -> int:
    results: list[OpsResult] = []
    for step_name, fn in [
        ("scripts", _install_scripts),
        ("cassandra launchagent", _install_cassandra_launchagent),
        ("homebrew web", _install_homebrew_web),
        ("log symlinks", _ensure_log_symlinks),
    ]:
        try:
            results += fn()
        except Exception as e:
            results.append(OpsResult(False, f"ops install failed: {step_name}", f"{type(e).__name__}: {e}"))

    ok = True
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.message}")
        if r.details:
            print(f"  {r.details}")
        ok = ok and r.ok

    return 0 if ok else 2


def status(args) -> int:
    print("myGPT ops status")

    if _which("brew"):
        cp = _run(["brew", "services", "list"], check=False)
        print("\nHomebrew services:\n" + (cp.stdout or "").strip())
    else:
        print("\nHomebrew services: brew not found")

    label = "com.mygpt.cassandra-logs"
    try:
        cp = _run(["launchctl", "list"], check=False)
        loaded = label in (cp.stdout or "")
        print(f"\nLaunchAgent {label}: {'LOADED' if loaded else 'NOT LOADED'}")
    except Exception as e:
        print(f"\nLaunchAgent {label}: ERROR ({e})")

    if _which("docker"):
        cp = _run(["docker", "ps", "--format", "{{.Names}}"], check=False)
        running = "mygpt-cassandra" in (cp.stdout or "")
        print(f"\nDocker container mygpt-cassandra: {'RUNNING' if running else 'NOT RUNNING'}")
    else:
        print("\nDocker: docker not found")

    return 0


def doctor(args) -> int:
    issues: list[str] = []

    cfg = Path.home() / ".myGPT" / "config.ini"
    if not cfg.exists():
        issues.append(f"Missing config {cfg}")

    for name in ("run-web.sh", "follow-cassandra-logs.sh"):
        p = Path.home() / ".myGPT" / "scripts" / name
        if p.exists() and not os.access(p, os.X_OK):
            issues.append(f"Script not executable {p}")

    for tool in ("brew", "docker"):
        if _which(tool) is None:
            issues.append(f"Missing tool in PATH: {tool}")

    if issues:
        print("myGPT ops doctor: FAIL")
        for i in issues:
            print(f"- {i}")
        return 2

    print("myGPT ops doctor: OK")
    return 0
