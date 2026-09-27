"""Codex stdio entry: verify reviewed local files and pass no unrelated credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mercury.ashare_runtime import verify_runtime, subprocess_env


def main():
    config = json.loads((ROOT / "data/integrations/ashare-runtime.json").read_text(encoding="utf-8"))
    verify_runtime(config)
    env = subprocess_env(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.call([config["python"], config["launcher"]], cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
