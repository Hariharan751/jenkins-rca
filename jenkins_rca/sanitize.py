import re
import yaml
from pathlib import Path

_rules: list[dict] | None = None


def _load_rules() -> list[dict]:
    global _rules
    if _rules is None:
        rules_path = Path(__file__).parent.parent / "sanitize_rules.yml"
        with open(rules_path) as f:
            _rules = yaml.safe_load(f)["rules"]
    return _rules


def sanitize(text: str) -> tuple[str, list[str]]:
    """Returns (clean_text, list_of_redaction_names)."""
    redactions = []
    for rule in _load_rules():
        pattern = re.compile(rule["pattern"])
        if pattern.search(text):
            redactions.append(rule["name"])
            text = pattern.sub(rule["replace"], text)
    return text, redactions
