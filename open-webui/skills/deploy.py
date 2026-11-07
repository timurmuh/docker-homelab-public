#!/usr/bin/env python3
"""Publish the wiki skills to every consumer, from this repository.

The canonical copies are `open-webui/skills/<id>/SKILL.md` here.  Two sinks
receive them:

  1. Open WebUI, over its REST API.
  2. `~/.config/opencode/skills/<id>/SKILL.md`, which opencode discovers
     natively and which is git-ignored in the opencode config repo.

`--check` compares the canonical copy against both sinks and exits 1 if either
differs.  `--dry-run` reports the same differences and exits 0.  Unknown flags
are an error: a mistyped `--check` must not publish anything.

Credentials come from `open-webui/skills/.env` (mode 600, git-ignored):
    OPENWEBUI_URL=https://openwebui.homelab.example.com
    OPENWEBUI_API_KEY=...
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

SKILLS = pathlib.Path(__file__).resolve().parent
OPENCODE_SKILLS = pathlib.Path.home() / ".config" / "opencode" / "skills"

# Only these directories are published.  Anything else under skills/ is local
# opencode tooling (browser automation, code review) and stays local.
DEPLOYED = ("llm-wiki", "org-mcp-quirks")


class NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would carry the bearer token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise urllib.error.HTTPError(
                newurl, code, "refusing cross-host redirect", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(NoCrossHostRedirect)


def load_env() -> None:
    """Read .env, letting it win over a stale exported value."""
    env_file = SKILLS / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip("'\"")
        if key in os.environ and os.environ[key] != value:
            print(f"note: {key} from .env overrides the exported value", file=sys.stderr)
        os.environ[key] = value


def base_url() -> str:
    url = os.environ["OPENWEBUI_URL"].rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit(
            f"error: refusing {parsed.scheme}:// for a non-local host; "
            "the API key would travel in clear text"
        )
    return url


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def request(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base_url()}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['OPENWEBUI_API_KEY']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with OPENER.open(req, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"detail": body[:400]}
    except (urllib.error.URLError, http.client.HTTPException,
            TimeoutError, OSError) as error:
        # DNS, TLS, refused connection, a timeout, or a mid-response drop.
        # Returned rather than raised so one skill's failure does not hide
        # what happened to the other.
        return 0, {"detail": f"request failed: {error}"}


def local_sink(skill_id: str, content: str, publish: bool) -> str:
    """Sync the opencode copy. Returns 'up to date', 'created' or 'updated'."""
    path = OPENCODE_SKILLS / skill_id / "SKILL.md"
    if path.is_symlink():
        raise SystemExit(f"error: {path} is a symlink; refusing to write through it")
    existing = path.read_text() if path.exists() else None
    if existing == content:
        return "up to date"
    action = "create" if existing is None else "update"
    if not publish:
        return f"would {action}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"{action}d"


def remote_sink(skill_id: str, content: str, meta: dict[str, str], publish: bool) -> str:
    """Create or update the Open WebUI copy. Returns a short status phrase."""
    status, existing = request("GET", f"/api/v1/skills/id/{skill_id}")
    if status == 404:
        existing = None
    elif status != 200:
        raise RuntimeError(f"GET failed: {status} {existing}")
    elif not isinstance(existing, dict) or "content" not in existing \
            or "version_id" not in existing:
        raise RuntimeError(f"unexpected response shape: {existing!r}"[:300])

    version = meta.get("version", "unset")
    if existing is not None and existing["content"] == content:
        deployed = frontmatter(existing["content"]).get("version", "unset")
        return (f"up to date (repo {version}, deployed {deployed} "
                f"{existing['version_id'][:8]})")

    if existing is None:
        action = "create"
        payload = {
            "id": skill_id,
            "name": meta.get("name", skill_id),
            "description": meta.get("description", ""),
            "content": content,
            "meta": {},
            "is_active": True,
            "access_grants": [],
        }
        path = "/api/v1/skills/create"
    else:
        action = "update"
        payload = {
            "id": skill_id,
            "name": existing.get("name", skill_id),
            "description": meta.get("description", existing.get("description", "")),
            "content": content,
            "expected_version_id": existing["version_id"],
            "commit_message": f"deploy: {skill_id} {version}",
            "meta": existing.get("meta", {}),
            "is_active": True,
        }
        path = f"/api/v1/skills/id/{skill_id}/update"

    if not publish:
        return f"would {action} (repo {version})"

    status, result = request("POST", path, payload)
    if status != 200 or not isinstance(result, dict) or "version_id" not in result:
        raise RuntimeError(f"{action} failed: {status} {result}")

    # Round-trip check: if the server normalises content, every later run
    # would report drift and mint a new version. Fail now instead.
    status, stored = request("GET", f"/api/v1/skills/id/{skill_id}")
    if status != 200 or not isinstance(stored, dict) or stored.get("content") != content:
        raise RuntimeError("round-trip mismatch: the server did not store the "
                           "content byte-for-byte")

    deployed = frontmatter(stored["content"]).get("version", "unset")
    return (f"{action}d (repo {version}, deployed {deployed} "
            f"{result['version_id'][:8]})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish the wiki skills to Open WebUI and the opencode config.",
        allow_abbrev=False,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true",
                       help="report drift and exit 1 if any; change nothing")
    group.add_argument("--dry-run", action="store_true",
                       help="report what would change; exit 0")
    args = parser.parse_args()

    load_env()
    if not os.environ.get("OPENWEBUI_API_KEY"):
        print("error: OPENWEBUI_API_KEY is not set (see .env.example)", file=sys.stderr)
        return 2
    os.environ.setdefault("OPENWEBUI_URL", "https://openwebui.homelab.example.com")

    publish = not (args.check or args.dry_run)
    failures = 0

    for directory in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        skill_file = directory / "SKILL.md"
        if not skill_file.exists() or directory.name not in DEPLOYED:
            continue
        if directory.is_symlink() or directory.resolve() != (SKILLS / directory.name).resolve():
            print(f"{directory.name}: error: not a real directory under {SKILLS}",
                  file=sys.stderr)
            failures += 1
            continue

        skill_id = directory.name
        content = skill_file.read_text()
        meta = frontmatter(content)

        for label, sink in (
            ("local", lambda: local_sink(skill_id, content, publish)),
            ("webui", lambda: remote_sink(skill_id, content, meta, publish)),
        ):
            try:
                status = sink()
            except (RuntimeError, OSError) as error:
                print(f"{skill_id}: {label} FAILED: {error}", file=sys.stderr)
                failures += 1
                continue
            print(f"{skill_id}: {label} {status}")
            if args.check and status != "up to date" and not status.startswith("up to date"):
                failures += 1

    # Report the other direction: a skill that is deployed but no longer
    # managed here would otherwise stay live and unmanaged forever.
    status, listing = request("GET", "/api/v1/skills/list")
    if status == 200 and isinstance(listing, dict):
        for item in listing.get("items", []):
            if isinstance(item, dict) and item.get("id") not in DEPLOYED:
                print(f"note: {item.get('id')} is deployed but not managed here",
                      file=sys.stderr)

    if failures:
        print(f"\n{failures} problem(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
