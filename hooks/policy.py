"""Load the shared policy once per hook process."""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.path.expandvars(os.path.expanduser(os.environ.get(
    "GUARDRAILS_CONFIG", str(Path(__file__).resolve().parent.parent / "guardrails.example.json")))))
with CONFIG_PATH.open() as source:
    CONFIG = json.load(source)


def path(value):
    return os.path.realpath(os.path.expandvars(os.path.expanduser(value)))
