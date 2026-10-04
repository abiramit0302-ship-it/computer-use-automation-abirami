"""Fill {{namespace.name}} placeholders with concrete values at run time.

Artifacts store placeholders. Real values (inputs from the caller, secrets from the
secret store) are substituted in memory only, just before an action runs. Logs must
record the step as written, with placeholders intact, never the rendered version.
"""

import json
from typing import TypeVar

from pydantic import BaseModel

from cua.schema import TEMPLATE_RE

Values = dict[str, dict[str, str]]  # e.g. {"inputs": {"member_id": "12345"}}
M = TypeVar("M", bound=BaseModel)


class TemplateError(ValueError):
    """A placeholder had no value supplied."""


def _lookup(values: Values, namespace: str, name: str) -> str:
    try:
        return values[namespace][name]
    except KeyError:
        raise TemplateError(f"no value supplied for {namespace}.{name}") from None


def render(text: str, values: Values) -> str:
    """Fill every placeholder in a plain string."""
    return TEMPLATE_RE.sub(lambda m: _lookup(values, m.group(1), m.group(2)), text)


def render_model(model: M, values: Values) -> M:
    """Return a filled-in copy of a schema object (a Step, a Target...). The original is untouched."""
    def substitute(match):
        value = _lookup(values, match.group(1), match.group(2))
        return json.dumps(value)[1:-1]  # escape quotes and backslashes so the JSON stays valid
    rendered = TEMPLATE_RE.sub(substitute, model.model_dump_json())
    return type(model).model_validate_json(rendered)