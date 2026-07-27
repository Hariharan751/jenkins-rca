import re
import yaml
from pathlib import Path

_rules: list[dict] | None = None


def _load_rules() -> list[dict]:
    global _rules
    if _rules is None:
        rules_path = Path(__file__).parent.parent / "classifier_rules.yml"
        with open(rules_path) as f:
            _rules = yaml.safe_load(f)["rules"]
    return _rules


def classify(log_window: str) -> str:
    """Returns error class string: parse_error | canary_fail | java_runtime | ..."""
    for rule in _load_rules():
        for pattern in rule.get("patterns", []):
            if re.search(pattern, log_window, re.IGNORECASE):
                return rule["class"]
    return "unknown"
