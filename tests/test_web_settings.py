"""Server defaults and score precision must be accepted by the browser form."""

from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.core import DEFAULTS


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "id" in attrs:
            self.fields[attrs["id"]] = attrs


def accepts_number(attrs, value):
    value = Decimal(str(value))
    minimum = Decimal(attrs.get("min", "0"))
    maximum = Decimal(attrs.get("max", "Infinity"))
    if not minimum <= value <= maximum:
        return False
    if attrs.get("step") == "any":
        return True
    # HTML number inputs use min as the step base when min is present.
    base = Decimal(attrs.get("min", attrs.get("value", "0")))
    return (value - base) % Decimal(attrs.get("step", "1")) == 0


@pytest.mark.parametrize("mode", ["engine_glow", "legacy"])
def test_server_thresholds_are_valid_in_settings_form(mode):
    html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text()
    inputs = Inputs(html).fields
    prefix = "engine_" if mode == "engine_glow" else ""
    fields = {
        "candidate-threshold": DEFAULTS[prefix + "candidate_threshold"],
        "strong-threshold": DEFAULTS[prefix + "strong_threshold"],
        "warm-threshold": DEFAULTS["min_warm_score"],
    }
    for field, value in fields.items():
        assert accepts_number(inputs[field], value), (mode, field, value)
    # Scores are continuous; custom precision accepted by the API must remain usable.
    for field, value in zip(fields, [0.000125, 0.00125, 0.00035]):
        assert accepts_number(inputs[field], value), (mode, field, value)
