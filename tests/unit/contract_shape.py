"""Shape comparison of API responses against contract/examples (section 14.5.2)."""
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLES = os.path.join(REPO, "contract", "examples")

# Objects whose keys are data (agent keys, model names, tool names), not field names.
DYNAMIC_MAPS = {"nodes", "byModel", "tools", "skills", "slashCommands", "subagentTypes",
                "unknownTypes"}


def load_example(name):
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _same_type(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return True
    return type(expected) is type(actual)


def shape_errors(expected, actual, path="$", field=None):
    """List mismatches: every example key must exist with a compatible type.

    null on either side is compatible with anything, extra fields in `actual`
    are allowed (the frontend ignores unknown fields).
    """
    if expected is None or actual is None:
        return []
    if not _same_type(expected, actual):
        return ["%s: expected %s, got %s" % (path, type(expected).__name__, type(actual).__name__)]
    errors = []
    if isinstance(expected, dict):
        if field in DYNAMIC_MAPS:
            if expected and actual:
                sample = next(iter(expected.values()))
                for key, value in actual.items():
                    errors += shape_errors(sample, value, "%s[%r]" % (path, key))
            return errors
        for key, value in expected.items():
            if key not in actual:
                errors.append("%s.%s: missing" % (path, key))
                continue
            errors += shape_errors(value, actual[key], "%s.%s" % (path, key), key)
    elif isinstance(expected, list) and expected and actual:
        for index, item in enumerate(actual):
            errors += shape_errors(_sample_for(expected, item), item, "%s[%d]" % (path, index))
    return errors


def _sample_for(expected, item):
    """Lists mixing kinds (graph nodes and edges) compare each item with a sample of its kind."""
    if isinstance(item, dict) and "kind" in item:
        for sample in expected:
            if isinstance(sample, dict) and sample.get("kind") == item["kind"]:
                return sample
    return expected[0]
