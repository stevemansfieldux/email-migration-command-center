"""What credential will the SDK use? Reported, not guessed — the SDK resolves
ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile."""
import json
import os
import shutil
import subprocess
from pathlib import Path


def is_no_credential(exc: BaseException) -> bool:
    """The SDK raises TypeError at request-build time when nothing resolves at all —
    before any HTTP — as distinct from AuthenticationError for a credential that exists but is rejected."""
    return isinstance(exc, TypeError) and "resolve authentication method" in str(exc)


def config_dir() -> Path:
    return Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))


def profiles() -> list[str]:
    d = config_dir() / "configs"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def status() -> dict:
    k = os.environ.get("ANTHROPIC_API_KEY", "")
    out = {
        "api_key_set": bool(k),
        "api_key_shape": (k[:7] + "…" + f" ({len(k)} chars)") if k else None,
        "api_key_looks_wrong": bool(k) and not k.startswith("sk-ant-"),
        "auth_token_set": bool(os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "profile_env": os.environ.get("ANTHROPIC_PROFILE") or None,
        "profiles": profiles(),
        "ant_installed": shutil.which("ant") is not None,
        "ant_status": None,
        "source": "none",
    }
    if out["api_key_set"]:
        out["source"] = "ANTHROPIC_API_KEY (wins over any profile)"
    elif out["auth_token_set"]:
        out["source"] = "ANTHROPIC_AUTH_TOKEN"
    elif out["profiles"]:
        out["source"] = f"ant profile: {out['profile_env'] or 'active/default'}"
    if out["ant_installed"]:
        try:
            r = subprocess.run(["ant", "auth", "status"], capture_output=True, text=True, timeout=8)
            out["ant_status"] = (r.stdout or r.stderr).strip()[:800]
        except Exception as e:  # noqa: BLE001 — surface, don't crash the settings page
            out["ant_status"] = f"could not run ant: {e}"
    return out


def test_connection(model: str) -> tuple[bool, str]:
    """One tiny real request on the messages endpoint — the one the app actually uses.
    Says which error class if it fails, never the credential."""
    import anthropic
    try:
        r = anthropic.Anthropic().messages.create(model=model, max_tokens=5, messages=[{"role": "user", "content": "ping"}])
        return True, f"OK — {r.model} answered ({r.usage.input_tokens} in / {r.usage.output_tokens} out)"
    except anthropic.AuthenticationError:
        return False, "A credential was found but the API rejected it. Re-run `ant auth login` (tokens expire), or check the key."
    except TypeError as e:
        if not is_no_credential(e):
            raise
        return False, "No credential resolved on this machine. Run `ant auth login`, or set ANTHROPIC_API_KEY."
    except anthropic.PermissionDeniedError:
        return False, "Authenticated, but this credential can't use that model."
    except anthropic.NotFoundError:
        return False, f"Authenticated, but model '{model}' was not found."
    except anthropic.APIConnectionError:
        return False, "Network error reaching the API."
    except anthropic.APIStatusError as e:
        return False, f"API error {e.status_code}: {e.message}"
