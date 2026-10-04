import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness.config import Config
from harness.evals import build_case_prompt, load_cases, run_eval, score_case

CASES = Path(__file__).resolve().parent.parent / "evals" / "cases"


def skill(name, extra=""):
    md = f"---\nname: {name}\ndescription: Run things.\n---\n\n# {name}\n\nRun tests. {extra}\n"
    return {"action": "create", "scope": "project", "name": name, "description": "Run things.",
            "reason": "r", "confidence": 0.9, "skill_md": md}


class EvalTests(unittest.TestCase):
    def test_shipped_cases_load_and_build_prompts(self):
        cases = load_cases(CASES)
        self.assertGreaterEqual(len(cases), 5)
        for case in cases:
            prompt = build_case_prompt(case)
            self.assertIn(case["conversation"][0]["text"][:30], prompt)
        patch_case = next(c for c in cases if "patch" in c["name"])
        self.assertIn("run-tests (project, probation)", build_case_prompt(patch_case))

    def test_scoring(self):
        case = {"name": "x", "expect": {"action": "create", "must_include": ["PYTHONPATH"], "must_exclude": ["evil"]}}
        self.assertTrue(score_case(case, json.dumps(skill("a", "PYTHONPATH=src"))).passed)
        self.assertIn("missing expected", score_case(case, json.dumps(skill("a"))).failures[0])
        self.assertFalse(score_case(case, json.dumps({"action": "ignore"})).passed)
        self.assertFalse(score_case(case, "not json").passed)
        self.assertFalse(score_case(case, json.dumps(skill("a", "PYTHONPATH evil"))).passed)
        secret = score_case(case, json.dumps(skill("a", "PYTHONPATH token=sk-live-ABCDEF1234567890SECRETKEY")))
        self.assertFalse(secret.passed)

    def test_either_action_and_patch_target(self):
        either = {"name": "x", "expect": {"action": ["create", "ignore"]}}
        self.assertTrue(score_case(either, json.dumps({"action": "ignore"})).passed)
        patch = {"name": "p", "expect": {"action": "patch", "patch_target": "run-tests"}}
        proposal = {**skill("other"), "action": "patch", "target_name": "other"}
        self.assertIn("patched", score_case(patch, json.dumps(proposal)).failures[0])

    def test_run_eval_with_stub_reflector(self):
        code = "import sys,json; sys.stdin.read(); print(json.dumps({'action':'ignore'}))"
        config = Config(home=Path("."), reflect_command=(sys.executable, "-c", code))
        results = run_eval(config, CASES)
        by_name = {r.name: r.passed for r in results}
        self.assertTrue(by_name["02-trivial-ignore"])
        self.assertFalse(by_name["01-repeatable-workflow"])

    def test_missing_reflector_reports_failure(self):
        results = run_eval(Config(home=Path("."), reflect_command=()), CASES, only="trivial")
        self.assertFalse(results[0].passed)
        self.assertIn("reflector error", results[0].failures[0])


if __name__ == "__main__":
    unittest.main()
