"""Verify the reviewed local launcher and upstream source before starting MCP."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

COMMIT = "614b380606e18eaf35747a0c3b2f27ec551caaa3"

ENV_ALLOWLIST = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP",
                 "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def subprocess_env(environ):
    """Preserve host routing, but never expose proxy userinfo to provider errors."""
    env = {k: v for k, v in environ.items() if k.upper() in ENV_ALLOWLIST}
    for key, value in env.items():
        if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} or not value:
            continue
        try:
            proxy = urlsplit(value if "://" in value else "http://" + value)
            credentialed = proxy.username is not None or proxy.password is not None
        except ValueError:
            raise ValueError("invalid proxy routing configuration") from None
        if credentialed:
            raise ValueError("authenticated proxy URLs are unsupported: provider errors may expose credentials")
    return env


def verify_runtime(config):
    if config.get("commit") != COMMIT:
        raise ValueError("ashare runtime is not the reviewed commit")
    launcher = Path(config.get("launcher", ""))
    manifest = Path(config.get("source_manifest", ""))
    interpreter = Path(config.get("python", ""))
    if not all(p.is_absolute() and p.is_file() for p in (launcher, manifest, interpreter)):
        raise ValueError("ashare runtime paths missing or not absolute")
    root = launcher.resolve().parent
    if manifest.resolve().parent != root:
        raise ValueError("ashare manifest and launcher installation roots differ")
    for p, field in [(launcher, "launcher_sha256"), (manifest, "source_manifest_sha256")]:
        if hashlib.sha256(p.read_bytes()).hexdigest() != config.get(field):
            raise ValueError("ashare runtime launcher/manifest hash mismatch")
    if (root / "commit.txt").read_text(encoding="utf-8").strip() != COMMIT:
        raise ValueError("ashare upstream commit receipt mismatch")
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    verified = set()
    for item in entries:
        relative = Path(item["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root / "upstream"):
            raise ValueError("ashare source manifest path escapes upstream")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("ashare reviewed source changed")
        verified.add(path)
    python_files = set((root / "upstream" / "src").rglob("*.py"))
    if not python_files or not python_files.issubset(verified):
        raise ValueError("ashare source contains unreviewed Python modules")
    identity = {k: config[k] for k in ("commit", "launcher_sha256", "source_manifest_sha256")}
    extension = config.get("extension")
    if extension is not None:
        if not isinstance(extension, dict) or extension.get("version") != "eastmoney-timestamp-v1":
            raise ValueError("unsupported ashare extension version")
        expected = {"extensions/timestamp-v1/ashare_timestamp_server.py",
                    "extensions/timestamp-v1/mercury_eastmoney_extension.py"}
        files = extension.get("files")
        if not isinstance(files, list) or len(files) != 2 or {f.get("path") for f in files if isinstance(f, dict)} != expected:
            raise ValueError("ashare extension manifest incomplete")
        for item in files:
            path = (root / item["path"]).resolve()
            if not path.is_relative_to(root / "extensions" / "timestamp-v1") or not path.is_file():
                raise ValueError("ashare extension path invalid")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
                raise ValueError("ashare extension source changed")
        actual = {p.relative_to(root).as_posix() for p in (root / "extensions" / "timestamp-v1").rglob("*.py")}
        if actual != expected:
            raise ValueError("ashare extension contains unreviewed Python files")
        identity["extension"] = extension
    return identity
