"""Read the venture's ops-hub (EMOH). Over the GitHub API with a read-only token, like MRO's;
or from a local checkout when EMOH_PATH is set — same interface, no network, good for dev.

This is the venture's EMOH — stevemansfieldux/email-migration-ops-hub — never MRO's ops-hub."""
import base64
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx

REPO = os.environ.get("EMOH_REPO", "stevemansfieldux/email-migration-ops-hub")
API = "https://api.github.com"
_cache: dict[str, tuple[float, object]] = {}
TTL = 120


class NotConfigured(RuntimeError):
    pass


def _local() -> Optional[Path]:
    p = os.environ.get("EMOH_PATH", "").strip()
    return Path(p) if p else None


def _token() -> str:
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if not t:
        raise NotConfigured("GITHUB_TOKEN is not set (a read-only token scoped to the EMOH repo), and EMOH_PATH is not set either")
    return t


def _get(url: str) -> dict | list:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    r = httpx.get(url, headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    _cache[url] = (time.time(), data)
    return data


def clear_cache() -> None:
    _cache.clear()


def file_tree() -> list[str]:
    """Every file path in the repo."""
    if (lp := _local()):
        return sorted(str(p.relative_to(lp)) for p in lp.rglob("*") if p.is_file() and ".git" not in p.parts)
    tree = _get(f"{API}/repos/{REPO}/git/trees/HEAD?recursive=1")
    return sorted(i["path"] for i in tree.get("tree", []) if i["type"] == "blob")


def read(path: str) -> str:
    if (lp := _local()):
        return (lp / path).read_text()
    data = _get(f"{API}/repos/{REPO}/contents/{path}")
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def frontmatter(text: str) -> dict:
    """Tiny YAML-ish reader: flat key: value pairs, [a, b] lists. Enough for our files."""
    m = FM.match(text)
    out: dict = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        out[k.strip()] = v
    return out


def list_meetings() -> list[dict]:
    """type: meeting files under meetings/, newest first. Notes and other types are skipped."""
    out = []
    for p in file_tree():
        if not (p.startswith("meetings/") and p.endswith(".md")):
            continue
        text = read(p)
        fm = frontmatter(text)
        if fm.get("type", "meeting") != "meeting":
            continue
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p)
        out.append({"path": p, "title": title, "date": fm.get("date"), "attendees": fm.get("attendees", []), "chars": len(text)})
    return sorted(out, key=lambda m: (m["date"] or "", m["path"]), reverse=True)
