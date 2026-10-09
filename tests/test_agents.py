import unittest
from datetime import date

from diva_atelier.agents import (ApprovalAgent, ApprovalError, GenerationError, InsightsAgent,
                                 PlannerAgent, QAAgent, ReportOrchestrator, WriterAgent,
                                 apply_recommendation, apply_terminology)
from diva_atelier.agents.context import ContextAgent, ContextBundle, Snippet
from diva_atelier.agents.insights import word_replacements
from diva_atelier.agents.qa import has_table, split_paragraphs
from diva_atelier.llm import LLMError, MockLLM
from diva_atelier.retrieval import InMemoryRetriever, load_knowledge
from diva_atelier.schema import parse_config, validate_config
from helpers import ROOT, ScriptedLLM, load_config, load_feedback, load_vars

TODAY = date(2025, 7, 1)


def knowledge():
    return load_knowledge(ROOT / "data" / "knowledge")


class PlannerTests(unittest.TestCase):
    def plan(self, key="bridal", **over):
        cfg = parse_config(load_config(key), key)
        return PlannerAgent().plan(cfg, {**load_vars(key), **over}, today=TODAY)

    def test_condition_includes_or_excludes_child_section(self):
        on = [s.section.num for s in self.plan(made_to_measure_offered=True).included]
        off = self.plan(made_to_measure_offered=False)
        self.assertIn("3.2", on)
        self.assertNotIn("3.2", [s.section.num for s in off.included])
        self.assertIn("condition not met", next(s.reason for s in off.steps if s.section.num == "3.2"))

    def test_hierarchy_order_and_depth(self):
        steps = self.plan().steps
        nums = [s.section.num for s in steps]
        self.assertEqual(nums, ["ES", "1", "2", "3", "3.1", "3.2"])
        self.assertEqual([s.depth for s in steps], [0, 0, 0, 0, 1, 1])

    def test_variable_validation(self):
        cases = {
            "collection_season": ({"start": "2025-06-30", "end": "2025-04-01"}, "after its end"),
        }
        for var, (bad, msg) in cases.items():
            self.assertTrue(any(msg in e for e in self.plan(**{var: bad}).errors), var)
        self.assertTrue(any("spans" in e for e in
                            self.plan(collection_season={"start": "2025-01-01", "end": "2025-06-30"}).errors))
        self.assertTrue(any("future" in e for e in
                            self.plan(collection_season={"start": "2025-06-20", "end": "2025-07-20"}).errors))
        self.assertTrue(any("one of" in e for e in self.plan(report_quarter="Q9").errors))
        self.assertTrue(any("required" in e for e in self.plan(collection_name="").errors))

    def test_extra_variables_warn_but_do_not_fail(self):
        p = self.plan(surprise="x")
        self.assertFalse(p.errors)
        self.assertTrue(any("surprise" in w for w in p.warnings))

    def test_number_and_boolean_types(self):
        p = PlannerAgent().plan(parse_config(load_config("everyday")), {"reporting_year": "abc", "label_name": "L"})
        self.assertTrue(any("number" in e for e in p.errors))


class ContextTests(unittest.TestCase):
    def section(self, **src):
        cfg = load_config("bridal")
        cfg["sections"][1]["sources"][0].update(src)
        return parse_config(cfg).sections[1]

    def test_retrieves_for_rendered_query(self):
        b = ContextAgent(knowledge()).gather(self.section(), load_vars("bridal"))
        self.assertIn("pr-brd-001", [s.doc_id for s in b.snippets])

    def test_fallbacks(self):
        empty = ContextAgent(InMemoryRetriever())
        v = load_vars("bridal")
        self.assertEqual(empty.gather(self.section(fallback="Skip"), v).warnings, [])
        self.assertTrue(empty.gather(self.section(fallback="Warn"), v).warnings)
        self.assertTrue(empty.gather(self.section(fallback="Escalate"), v).escalate)
        self.assertFalse(empty.gather(self.section(fallback="GenerateWithoutContext"), v).escalate)
        self.assertTrue(empty.gather(self.section(fallback="UseDefault"), v).warnings)   # no default set
        self.assertTrue(empty.gather(self.section(fallback="GenerateWithoutContext", isRequired=True), v).warnings)

    def test_use_default_content(self):
        cfg = load_config("bridal")
        cfg["sections"][1]["sources"][0]["fallback"] = "UseDefault"
        cfg["sections"][1]["sampleHtml"] = "Default overview text."
        b = ContextAgent(InMemoryRetriever()).gather(parse_config(cfg).sections[1], load_vars("bridal"))
        self.assertTrue(b.used_default)
        self.assertEqual(b.snippets[0].text, "Default overview text.")

    def test_unset_variable_skips_source(self):
        b = ContextAgent(knowledge()).gather(self.section(), {})
        self.assertFalse(b.snippets)
        self.assertTrue(any("unset variable" in w for w in b.warnings))


class WriterTests(unittest.TestCase):
    def test_terminology_is_whole_word_case_aware_and_longest_first(self):
        text, counts = apply_terminology("Cheap and cheaper. The tight fit is tight.",
                                         {"cheap": "accessibly priced", "tight fit": "fitted silhouette", "tight": "fitted"})
        self.assertEqual(text, "Accessibly priced and cheaper. The fitted silhouette is fitted.")
        self.assertEqual(counts, {"tight fit": 1, "cheap": 1, "tight": 1})

    def test_writer_applies_glossary_even_if_model_ignores_it(self):
        cfg = load_config("bridal")
        cfg["sections"][0]["styleTone"] = {"preferredTerminology": {"cheap": "accessibly priced"}, "maxParagraphs": 2}
        sec = parse_config(cfg).sections[0]
        llm = ScriptedLLM("The cheap range sold well.")
        draft = WriterAgent(llm).write(sec, load_vars("bridal"), ContextBundle())
        self.assertEqual(draft.text, "The accessibly priced range sold well.")
        system = llm.calls[0]["system"]
        self.assertIn("at most 2 paragraphs", system)
        self.assertIn("say 'accessibly priced' not 'cheap'", system)

    def test_prompt_contains_numbered_context_and_revision_notes(self):
        sec = parse_config(load_config("bridal")).sections[1]
        llm = ScriptedLLM("ok")
        ctx = ContextBundle(snippets=[Snippet("d1", "Aurelia facts.", 0.9, "src1", "prior-reports")])
        WriterAgent(llm).write(sec, load_vars("bridal"), ctx, ["Too long."])
        prompt = llm.calls[0]["prompt"]
        self.assertIn("[1] Aurelia facts.", prompt)
        self.assertIn("- Too long.", prompt)
        self.assertIn("Aurelia" if False else "2025-04-01 – 2025-06-30", prompt)    # variables rendered


class QATests(unittest.TestCase):
    def review(self, text, mutate=None, shape=None):
        cfg = load_config("bridal")
        sec = cfg["sections"][0]
        if shape:
            sec["shape"] = shape
        if mutate:
            mutate(sec)
        s = parse_config(cfg).sections[0]
        return QAAgent().review(s, text, load_vars("bridal"), ContextBundle())

    def codes(self, r):
        return {i.code for i in r.issues}

    def test_clean_draft_passes(self):
        r = self.review("The collection met its target for 2025-04-01 – 2025-06-30. Write a concise executive "
                        "summary covering key findings.")
        self.assertTrue(r.passed, r.to_dict())
        self.assertGreaterEqual(r.score, 90)

    def test_critical_issues_fail_regardless_of_score(self):
        r = self.review("Results for {collection_name} are strong.")
        self.assertIn("unresolved_placeholder", self.codes(r))
        self.assertFalse(r.passed)
        self.assertIn("empty_content", self.codes(self.review("   ")))

    def test_paragraph_limit_table_and_terminology(self):
        r = self.review("One.\n\nTwo.\n\nThree.", mutate=lambda s: s.update(styleTone={"maxParagraphs": 2}))
        self.assertIn("too_many_paragraphs", self.codes(r))
        self.assertIn("missing_table", self.codes(self.review("Prose only.", shape="Table")))
        self.assertNotIn("missing_table", self.codes(self.review("Lead.\n\n| A | B |\n| - | - |\n| 1 | 2 |", shape="Table")))
        r = self.review("A cheap gown.", mutate=lambda s: s.update(styleTone={"preferredTerminology": {"cheap": "accessibly priced"}}))
        self.assertIn("forbidden_term", self.codes(r))

    def test_ungrounded_numbers_and_checklist(self):
        r = self.review("Sell-through reached 83% across 41 styles.")
        self.assertIn("ungrounded_numbers", self.codes(r))
        r = self.review("Short text.", mutate=lambda s: s.update(qualityConfig={"validationChecklist": ["Mentions silhouettes and hemlines"]}))
        self.assertIn("checklist_unmet", self.codes(r))

    def test_weights_change_the_score(self):
        text = "x " * 200 + "."                                    # very long sentence hurts clarity
        base = self.review(text).score
        heavy = self.review(text, mutate=lambda s: s.update(
            qualityConfig={"scoringWeights": {"clarity": 1.0}})).score
        self.assertLess(heavy, base)

    def test_llm_judge_is_blended_and_failure_is_tolerated(self):
        s = parse_config(load_config("bridal")).sections[0]
        good = QAAgent(MockLLM(), use_llm_judge=True).review(s, "Write a concise executive summary covering key findings.", {}, ContextBundle())
        self.assertTrue(good.dimensions["accuracy"] > 0)
        bad = QAAgent(ScriptedLLM("not json"), use_llm_judge=True).review(s, "Some text here.", {}, ContextBundle())
        self.assertIsNotNone(bad.score)
        err = QAAgent(ScriptedLLM("x", error=LLMError("down")), use_llm_judge=True).review(s, "Some text.", {}, ContextBundle())
        self.assertIsNotNone(err.score)

    def test_helpers(self):
        self.assertEqual(len(split_paragraphs("A.\n\n| a | b |\n| - | - |\n\n# H\n\nB.")), 2)
        self.assertTrue(has_table("| a | b |\n| --- | --- |\n| 1 | 2 |"))


class OrchestratorTests(unittest.TestCase):
    def orch(self, llm=None, **kw):
        return ReportOrchestrator(llm or MockLLM(), knowledge(), id_factory=lambda: "GR-TEST", **kw)

    def test_end_to_end_for_every_shipped_configuration(self):
        for key in ("bridal", "evening", "cocktail", "everyday"):
            r = self.orch().generate(load_config(key), load_vars(key), key=key, author="priya")
            self.assertEqual(r.status, "ready_for_review", (key, [(s.num, s.status, s.qa and s.qa["issues"]) for s in r.sections]))
            self.assertTrue(all(s.content for s in r.sections))
            self.assertEqual(r.id, "GR-TEST")
            self.assertTrue(r.events)

    def test_skipped_sections_are_reported(self):
        r = self.orch().generate(load_config("bridal"), {**load_vars("bridal"), "made_to_measure_offered": False})
        self.assertNotIn("3.2", [s.num for s in r.sections])
        self.assertTrue(any("3.2" in s["section"] for s in r.skipped))

    def test_cocktail_retrieves_table_context(self):
        r = self.orch().generate(load_config("cocktail"), load_vars("cocktail"))
        results = next(s for s in r.sections if s.num == "2")
        self.assertTrue(results.context)
        self.assertIn("|", results.content)

    def test_revision_loop_feeds_qa_issues_back_to_the_writer(self):
        cfg = load_config("bridal")
        cfg["sections"][0]["styleTone"] = {"maxParagraphs": 1}
        cfg["sections"] = cfg["sections"][:1]
        llm = ScriptedLLM("Para one.\n\nPara two.", "Write a concise executive summary covering key findings.")
        r = self.orch(llm).generate(cfg, load_vars("bridal"))
        sec = r.sections[0]
        self.assertEqual((sec.status, sec.attempts), ("passed", 2))
        self.assertIn("paragraphs", llm.calls[1]["prompt"])

    def test_section_that_never_passes_is_flagged_not_hidden(self):
        cfg = load_config("bridal")
        cfg["sections"] = cfg["sections"][:1]
        cfg["sections"][0]["qualityConfig"] = {"maxRevisions": 1}
        r = self.orch(ScriptedLLM("Broken {collection_name}.")).generate(cfg, load_vars("bridal"))
        self.assertEqual((r.sections[0].status, r.sections[0].attempts, r.status), ("needs_review", 2, "needs_attention"))

    def test_llm_failure_is_contained_to_its_section(self):
        cfg = load_config("bridal")
        cfg["sections"] = cfg["sections"][:2]
        r = self.orch(ScriptedLLM("x", error=LLMError("boom"))).generate(cfg, load_vars("bridal"))
        self.assertEqual([s.status for s in r.sections], ["failed", "failed"])
        self.assertIn("boom", r.sections[0].warnings[0])

    def test_escalation_and_manual_sections(self):
        cfg = load_config("bridal")
        cfg["sections"][1]["sources"][0]["fallback"] = "Escalate"
        cfg["sections"][2]["generatable"] = False
        r = ReportOrchestrator(MockLLM(), InMemoryRetriever()).generate(cfg, load_vars("bridal"))
        status = {s.num: s.status for s in r.sections}
        self.assertEqual((status["1"], status["2"]), ("escalated", "manual"))

    def test_invalid_config_and_inputs_raise_with_reasons(self):
        bad = load_config("bridal")
        bad["sections"][0]["parent"] = "ghost"
        with self.assertRaises(GenerationError) as c:
            self.orch().generate(bad, load_vars("bridal"))
        self.assertTrue(any("ghost" in p for p in c.exception.problems))
        with self.assertRaises(GenerationError) as c:
            self.orch().generate(load_config("bridal"), {})
        self.assertGreaterEqual(len(c.exception.problems), 3)

    def test_token_usage_is_summed(self):
        r = self.orch(ScriptedLLM("Write a concise executive summary covering key findings.")).generate(
            {**load_config("bridal"), "sections": load_config("bridal")["sections"][:1]}, load_vars("bridal"))
        self.assertEqual(r.usage, {"input_tokens": 10, "output_tokens": 5})


class ApprovalTests(unittest.TestCase):
    def build(self, auto=False, score=95.0, status="passed", key="evening"):
        cfg = parse_config(load_config(key), key)
        res = [{"section_id": s.id, "status": status, "qa_score": score} for s in cfg.sections]
        agent = ApprovalAgent()
        return agent, agent.build(cfg, res, author="priya", auto_approve=auto)

    def test_global_stage_is_gated_by_section_stage_and_order(self):
        agent, wf = self.build()
        sec = next(r for r in wf.requests if r.scope == "section")
        glob = [r for r in wf.requests if r.scope == "global"]
        self.assertFalse(wf.actionable(glob[0]))
        with self.assertRaises(ApprovalError):
            agent.act(wf, glob[0].id, "dana", glob[0].role, "approve")
        agent.act(wf, sec.id, "mark", "ProductionManager", "approve")
        self.assertTrue(wf.actionable(glob[0]))
        self.assertFalse(wf.actionable(glob[1]))                    # order 2 waits for order 1
        agent.act(wf, glob[0].id, "mark2", "ProductionManager", "approve")
        agent.act(wf, glob[1].id, "qc", "QualityController", "approve")
        self.assertEqual(len([r for r in wf.requests if r.scope == "global"]), 3)   # numApproversRequired = 3
        self.assertEqual(wf.status, "in_review")
        agent.act(wf, glob[2].id, "anyone", "Intern", "approve")    # generic slot takes any role
        self.assertEqual(wf.status, "approved")

    def test_rules_roles_authors_duplicates_and_rejection(self):
        agent, wf = self.build()
        sec = next(r for r in wf.requests if r.scope == "section")
        with self.assertRaisesRegex(ApprovalError, "needs the ProductionManager"):
            agent.act(wf, sec.id, "x", "Writer", "approve")
        with self.assertRaisesRegex(ApprovalError, "own report"):
            agent.act(wf, sec.id, "priya", "ProductionManager", "approve")
        with self.assertRaisesRegex(ApprovalError, "comment is required"):
            agent.act(wf, sec.id, "mark", "ProductionManager", "reject")
        agent.act(wf, sec.id, "mark", "ProductionManager", "reject", "Fabric notes missing.")
        self.assertEqual(wf.status, "changes_requested")
        with self.assertRaises(ApprovalError):
            agent.act(wf, sec.id, "mark", "ProductionManager", "approve")           # already decided

    def test_same_person_cannot_take_two_global_slots(self):
        agent, wf = self.build()
        agent.act(wf, next(r.id for r in wf.requests if r.scope == "section"), "mark", "ProductionManager", "approve")
        g = [r for r in wf.requests if r.scope == "global"]
        agent.act(wf, g[0].id, "dana", "ProductionManager", "approve")
        agent.act(wf, g[1].id, "qc", "QualityController", "approve")
        with self.assertRaisesRegex(ApprovalError, "already approved"):
            agent.act(wf, g[2].id, "dana", "Any", "approve")

    def test_auto_approve_only_when_passed_and_above_threshold(self):
        _, wf = self.build(auto=True, score=95)
        self.assertEqual(next(r for r in wf.requests if r.scope == "section").status, "auto_approved")
        _, wf = self.build(auto=True, score=80)
        self.assertEqual(next(r for r in wf.requests if r.scope == "section").status, "pending")
        _, wf = self.build(auto=True, score=99, status="needs_review")
        self.assertEqual(next(r for r in wf.requests if r.scope == "section").status, "pending")
        _, wf = self.build(auto=False, score=99)
        self.assertEqual(next(r for r in wf.requests if r.scope == "section").status, "pending")

    def test_round_trip_and_overdue(self):
        _, wf = self.build()
        from datetime import datetime, timedelta, timezone
        from diva_atelier.agents import ApprovalWorkflow
        again = ApprovalWorkflow.from_dict(wf.to_dict())
        self.assertEqual(again.to_dict(), wf.to_dict())
        self.assertEqual(len(wf.overdue(datetime.now(timezone.utc) + timedelta(days=30))), len(wf.requests))
        self.assertEqual(wf.overdue(datetime.now(timezone.utc)), [])


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_config("bridal")
        self.cfg = parse_config(self.raw, "bridal")
        self.recs = InsightsAgent().analyze(self.cfg, load_feedback())

    def titles(self):
        return [r.title for r in self.recs]

    def test_finds_all_three_patterns_from_shipped_feedback(self):
        t = " | ".join(self.titles())
        self.assertIn("'cheap' → 'accessibly priced'", t)
        self.assertIn("Executive Summary", t)
        self.assertIn("Style Performance", t)
        self.assertIn("maxParagraphs from 2 to 3", t)

    def test_recommendation_shape_matches_dashboard(self):
        d = self.recs[0].to_dict()
        for k in ("id", "type", "status", "priority", "section", "sectionTitle", "title", "summary",
                  "confidence", "patternBasis", "patternTags", "feedbackThread", "aiChange"):
            self.assertIn(k, d)
        self.assertEqual(set(d["aiChange"]), {"field", "label", "before", "after"})
        self.assertTrue(0 < d["confidence"] <= 1)

    def test_single_occurrences_are_not_patterns(self):
        one = [f for f in load_feedback() if f["id"] in ("edit-1", "edit-2", "edit-3")]
        self.assertEqual(InsightsAgent().analyze(self.cfg, one), [])
        self.assertTrue(InsightsAgent(min_occurrences=1).analyze(self.cfg, one))

    def test_word_replacement_detection(self):
        self.assertEqual(word_replacements("The cheap range.", "The accessibly priced range."),
                         [("cheap", "accessibly priced")])
        self.assertEqual(word_replacements("a b c", "a b c d"), [])

    def test_apply_every_recommendation_keeps_config_valid_and_bumps_version(self):
        cfg = self.raw
        for rec in self.recs:
            cfg = apply_recommendation(cfg, rec.to_dict())
        self.assertEqual([i for i in validate_config(cfg) if i.severity == "error"], [])
        self.assertEqual(cfg["version"], self.raw["version"] + len(self.recs))
        self.assertEqual(cfg["status"], "Draft")
        es = next(s for s in cfg["sections"] if s["num"] == "ES")
        self.assertEqual(es["styleTone"]["preferredTerminology"], {"cheap": "accessibly priced"})
        self.assertEqual(next(s for s in cfg["sections"] if s["num"] == "1")["styleTone"]["maxParagraphs"], 3)
        self.assertEqual(self.raw["version"], 3)                    # original untouched

    def test_applied_feedback_is_not_recommended_again(self):
        cfg = self.raw
        consumed = set()
        for rec in self.recs:
            d = rec.to_dict()
            cfg = apply_recommendation(cfg, d)
            consumed |= set(d["patternBasis"])
        cfg["consumedFeedback"] = sorted(consumed)
        self.assertEqual(InsightsAgent().analyze(parse_config(cfg, "bridal"), load_feedback()), [])

    def test_applying_terminology_changes_generated_output(self):
        before = ReportOrchestrator(MockLLM(), knowledge()).generate(self.raw, load_vars("bridal"))
        self.assertIn("cheap", before.sections[1].content)
        rec = next(r for r in self.recs if "cheap" in r.title)
        after_cfg = apply_recommendation(self.raw, rec.to_dict())
        after_cfg["sections"][1]["styleTone"] = after_cfg["sections"][0]["styleTone"]      # glossary on section 1 too
        after = ReportOrchestrator(MockLLM(), knowledge()).generate(after_cfg, load_vars("bridal"))
        self.assertNotIn("cheap", after.sections[1].content)
        self.assertIn("accessibly priced", after.sections[1].content)

    def test_unsupported_change_path_is_rejected(self):
        d = self.recs[0].to_dict()
        d["aiChange"]["field"] = "name"
        with self.assertRaises(ValueError):
            apply_recommendation(self.raw, d)
        d["aiChange"]["field"] = "sections[ghost].styleTone.maxParagraphs"
        with self.assertRaises(ValueError):
            apply_recommendation(self.raw, d)


if __name__ == "__main__":
    unittest.main()
