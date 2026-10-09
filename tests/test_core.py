import unittest

from diva_atelier.conditions import evaluate
from diva_atelier.retrieval import Document, InMemoryRetriever, load_knowledge
from diva_atelier.schema import Condition, parse_config, validate_config
from diva_atelier.templating import find_placeholders, render
from helpers import ROOT, load_config


class ConditionTests(unittest.TestCase):
    def test_boolean_and_text_forms_are_equivalent(self):
        c = Condition("mtm", "equals", "Yes")
        for truthy in (True, "Yes", "true", "TRUE", "y"):
            self.assertTrue(evaluate(c, {"mtm": truthy}), truthy)
        for falsy in (False, "No", "false", None, ""):
            self.assertFalse(evaluate(c, {"mtm": falsy}), falsy)

    def test_operators(self):
        self.assertTrue(evaluate(Condition("q", "in", "Q1,Q2"), {"q": "Q2"}))
        self.assertTrue(evaluate(Condition("q", "notIn", ["Q1"]), {"q": "Q4"}))
        self.assertTrue(evaluate(Condition("q", "notEquals", "Q1"), {"q": "Q2"}))
        self.assertTrue(evaluate(Condition("q", "isNotNull"), {"q": "x"}))
        self.assertTrue(evaluate(Condition("q", "isEmpty"), {}))
        self.assertTrue(evaluate(None, {}))

    def test_unknown_operator_raises(self):
        with self.assertRaises(ValueError):
            evaluate(Condition("q", "approx", "1"), {"q": 1})


class TemplatingTests(unittest.TestCase):
    def test_render_formats_values_and_reports_missing(self):
        out, missing = render("{a} {b} {c} {d} {e}", {"a": True, "b": {"start": "2025-01-01", "end": "2025-03-31"},
                                                     "c": ["x", "y"], "d": 2024})
        self.assertEqual(out, "Yes 2025-01-01 – 2025-03-31 x, y 2024 {e}")
        self.assertEqual(missing, ["e"])

    def test_find_placeholders_dedupes_in_order(self):
        self.assertEqual(find_placeholders("{b} {a} {b} {1x}"), ["b", "a"])


class SchemaTests(unittest.TestCase):
    def test_all_shipped_configs_validate_cleanly(self):
        for p in sorted((ROOT / "configs").glob("*.json")):
            import json
            issues = validate_config(json.loads(p.read_text(encoding="utf-8")))
            self.assertEqual([i.message for i in issues if i.severity == "error"], [], p.name)

    def test_parse_exposes_structure(self):
        cfg = parse_config(load_config("bridal"), "bridal")
        self.assertEqual(cfg.section("3.2").condition.variable, "made_to_measure_offered")
        self.assertIs(cfg.section("3.1"), next(s for s in cfg.sections if s.id == "s5"))

    def test_detects_each_class_of_problem(self):
        cfg = load_config("bridal")
        cfg["variables"].append(dict(cfg["variables"][0]))                       # duplicate name
        cfg["sections"][1]["instructions"] += " {nope}"                          # undefined placeholder
        cfg["sections"][2]["parent"] = "ghost"                                   # missing parent
        cfg["sections"][3]["condition"] = {"variable": "ghost", "operator": "equals", "value": "x"}
        cfg["sections"][4]["qualityConfig"] = {"scoringWeights": {"accuracy": 0.9, "clarity": 0.9}}
        msgs = " | ".join(i.message for i in validate_config(cfg) if i.severity == "error")
        for needle in ("Duplicate variable", "undefined variable {nope}", "Parent 'ghost'",
                       "undefined variable 'ghost'", "sum to 1.0"):
            self.assertIn(needle, msgs)

    def test_detects_hierarchy_cycle_and_bad_source(self):
        cfg = load_config("bridal")
        cfg["sections"][4]["parent"] = "s5"                                      # s5 is its own parent
        cfg["sections"][1]["sources"][0]["minScore"] = 7
        msgs = " | ".join(i.message for i in validate_config(cfg) if i.severity == "error")
        self.assertIn("cycle", msgs)
        self.assertIn("minScore must be between 0 and 1", msgs)

    def test_missing_top_level_fields_short_circuit(self):
        self.assertEqual({i.path for i in validate_config({})}, {"name", "variables", "sections"})


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.r = InMemoryRetriever([
            Document("a", "idx", "Aurelia bridal collection overview and background."),
            Document("b", "idx", "Evening gown showroom traffic peaks before the carnival."),
            Document("c", "idx", "Cocktail sell-through table", content_class="Table"),
            Document("z", "other", "Aurelia bridal collection overview"),
        ])

    def test_ranks_relevant_first_and_respects_index(self):
        hits = self.r.search(index="idx", query="aurelia collection overview", top_k=5)
        self.assertEqual(hits[0].doc.id, "a")
        self.assertNotIn("z", [h.doc.id for h in hits])

    def test_min_score_and_top_k_and_content_class(self):
        self.assertEqual(self.r.search(index="idx", query="aurelia collection overview", min_score=0.99), [])
        self.assertLessEqual(len(self.r.search(index="idx", query="collection showroom", top_k=1)), 1)
        got = self.r.search(index="idx", query="cocktail sell-through", content_class="Table")
        self.assertEqual([h.doc.id for h in got], ["c"])

    def test_modes_and_empty_query(self):
        for mode in ("Keyword", "Semantic", "Hybrid", "MetadataOnly"):
            self.assertTrue(self.r.search(index="idx", query="aurelia", mode=mode), mode)
        self.assertEqual(self.r.search(index="idx", query="the of and"), [])

    def test_loads_shipped_knowledge(self):
        r = load_knowledge(ROOT / "data" / "knowledge")
        self.assertTrue(r.search(index="prior-reports", query="collection overview background Aurelia", min_score=0.7))
        self.assertTrue(r.search(index="sales-data", query="cocktail dress sell-through 10482377 Q2 Apr Jun",
                                 mode="Hybrid", min_score=0.7, content_class="Table"))


if __name__ == "__main__":
    unittest.main()
