"""Every worker schema must satisfy the structured-output strict-mode contract.

On 2026-09-19, the first day research ever drew compute, all eight industries failed at
the discovery stage inside a second. The worker log carried a 400 from the provider:

    invalid_json_schema: In context=(), 'required' is required to be supplied and to be
    an array including every key in properties. Missing 'inability'.
    param: text.format.schema

`discovery-schema.json` and `research-schema.json` both declared an optional `inability`
property - the very field the prompt asks the worker to fill in when it cannot finish -
and strict mode rejects any object whose `required` omits a declared property. The
request never reached a model, so no worker could report a diagnostic; the coordinator
then saw eight workers exit with no identified failure and abandoned each industry,
terminalising the research day.

Two properties are pinned here, over the REAL files rather than fixtures, because a
fixture would have passed happily while production 400ed:

  1. strict-mode conformance for every schema the workers use, so the next schema added
     cannot reintroduce this;
  2. `inability` stays nullable, so "return inability=null on success" still validates.
"""

import profile_home  # noqa: F401 (a temporary SIDEDOOR_HOME, never the real one; E6.T2 P15)
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Every schema handed to a worker as a structured-output contract.
SCHEMAS = sorted(ROOT.glob("*schema*.json"))
# The two that gate research: discovery names companies, research qualifies them.
RESEARCH_SCHEMAS = ("discovery-schema.json", "research-schema.json")


def objects(node, path="(root)"):
    """Yield (path, object-schema) for every object node, including inside anyOf/items."""
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        yield path, node
    for name, child in node.get("properties", {}).items():
        yield from objects(child, f"{path}/{name}")
    if isinstance(node.get("items"), dict):
        yield from objects(node["items"], path + "[]")
    for keyword in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(node.get(keyword, [])):
            yield from objects(child, f"{path}/{keyword}[{index}]")


class StructuredOutputSchemas(unittest.TestCase):
    def test_there_are_schemas_to_check(self):
        """Guards the whole file: a bad glob would make every test below vacuous."""
        self.assertGreaterEqual(len(SCHEMAS), 7, [p.name for p in SCHEMAS])
        for name in RESEARCH_SCHEMAS:
            self.assertIn(name, [p.name for p in SCHEMAS])

    def test_every_declared_property_is_required(self):
        """The exact rule the provider enforces, and the one that broke research."""
        for schema in SCHEMAS:
            document = json.loads(schema.read_text())
            for path, node in objects(document):
                declared = list(node.get("properties", {}))
                if not declared:
                    continue
                required = node.get("required")
                self.assertIsInstance(required, list, f"{schema.name} {path}: 'required' must be a list")
                self.assertEqual(
                    [name for name in declared if name not in required], [],
                    f"{schema.name} {path}: strict mode rejects a declared property that is "
                    f"not in 'required'; the provider returns 400 invalid_json_schema and no "
                    f"model ever runs")

    def test_every_object_forbids_additional_properties(self):
        for schema in SCHEMAS:
            document = json.loads(schema.read_text())
            for path, node in objects(document):
                if node.get("properties"):
                    self.assertIs(node.get("additionalProperties"), False,
                                  f"{schema.name} {path}: strict mode requires additionalProperties false")

    def test_inability_stays_nullable_in_both_research_schemas(self):
        """Required is not the same as non-null: the prompt asks for null on success."""
        for name in RESEARCH_SCHEMAS:
            document = json.loads((ROOT / name).read_text())
            self.assertIn("inability", document["properties"], name)
            self.assertIn("inability", document["required"], name)
            branches = document["properties"]["inability"].get("anyOf", [])
            self.assertIn({"type": "null"}, branches,
                          f"{name}: a worker that finishes cleanly reports inability=null")
            self.assertTrue(any(branch.get("type") == "object" for branch in branches),
                            f"{name}: a worker that cannot finish reports a structured inability")

    def test_the_inability_object_still_carries_the_fields_the_coordinator_reads(self):
        """research_recovery.validate requires these; a nullable branch must not drop them."""
        for name in RESEARCH_SCHEMAS:
            document = json.loads((ROOT / name).read_text())
            branch = next(b for b in document["properties"]["inability"]["anyOf"]
                          if b.get("type") == "object")
            self.assertEqual(
                set(branch["required"]),
                {"error_class", "diagnostic", "attempted_methods", "required_input", "progress"},
                name)


if __name__ == "__main__":
    unittest.main()
