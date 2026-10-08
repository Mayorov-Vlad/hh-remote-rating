import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path = ROOT / "config.toml") -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("db", "reports", "curation"):
        cfg["paths"][key] = ROOT / cfg["paths"][key]
    return cfg
