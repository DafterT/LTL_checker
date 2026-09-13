"""Проверки самого скрипта: python -m unittest -v test_runner.py.

Интеграционные проверки используют установленный SPIN и реальные .trail.
Тестовые отчёты остаются в build/, основной report.txt не перезаписывается.
"""

import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import run


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (run.ROOT / "build").mkdir(exist_ok=True)
        cls.folder = Path(tempfile.mkdtemp(prefix="runner_checks_", dir=run.ROOT / "build"))

    def cli(self, label, *arguments):
        report = self.folder / f"{label}.txt"
        process = subprocess.run(
            [sys.executable, str(run.ROOT / "run.py"), "--report", str(report), *arguments],
            cwd=run.ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
        )
        text = report.read_text(encoding="utf-8")
        self.assertNotIn("Traceback", process.stdout + process.stderr)
        return process, text

    def test_wrong_positive_expectation_produces_replayable_counterexample(self):
        process, report = self.cli(
            "wrong_positive", "--test", "*both_reset/late_reset", "--expect", "true")
        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
        self.assertIn("[FAIL]", report)
        self.assertIn("t=2: a=1, b=1", report)
        self.assertNotIn("t=3:", report)  # SPIN останавливается до запоздалого сброса.
        folder = Path(re.search(r"^  Файлы: (.+)$", report, re.MULTILINE).group(1))
        self.assertTrue((folder / "model.pml.trail").is_file())
        self.assertIn("assertion violated", (folder / "verify.log").read_text(encoding="utf-8"))
        replay = subprocess.run(
            [sys.executable, str(run.ROOT / "run.py"), "--replay", str(folder)],
            cwd=run.ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("t=2: a=1, b=1", replay.stdout)
        self.assertIn("t=1: a=1, b=1", replay.stdout)

    def test_liveness_counterexample_contains_cycle(self):
        process, report = self.cli(
            "liveness", "--test", "*marriage_twice/once_later", "--expect", "true")
        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
        self.assertIn("[FAIL]", report)
        self.assertIn("начало повторяемого цикла SPIN", report)
        self.assertEqual(len(re.findall(r"t=\d+: q=1", report)), 1)

    def test_wrong_negative_expectation_does_not_invent_counterexample(self):
        process, report = self.cli(
            "wrong_negative", "--test", "*marriage_twice/twice_adjacent", "--expect", "false")
        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
        self.assertIn("Получено: true", report)
        self.assertNotIn("Контрпример из SPIN", report)
        folder = Path(re.search(r"^  Файлы: (.+)$", report, re.MULTILINE).group(1))
        self.assertFalse((folder / "model.pml.trail").exists())

    def test_depth_limit_is_error_even_when_violation_was_expected(self):
        process, report = self.cli("depth", "--test", "*both_reset/late_reset", "--depth", "1")
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn("[ERROR]", report)
        self.assertNotIn("[PASS]", report)
        self.assertIn("глубин", report)

    def test_invalid_formula_does_not_prevent_next_test(self):
        folder = self.folder / "mixed_tasks"
        folder.mkdir()
        task = copy.deepcopy(next(task for task in run.load_tasks(run.ROOT / "tasks")
                                  if task["predicates"] == ["a", "b"]))
        task["tests"] = [task["tests"][0]]
        (folder / "01_valid.json").write_text(json.dumps(task), encoding="utf-8")
        task["formula"] = "[] ("
        task["tests"][0]["expected"] = False
        (folder / "00_invalid.json").write_text(json.dumps(task), encoding="utf-8")
        process, report = self.cli("mixed", "--tasks", str(folder))
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn("[ERROR] 00_invalid/", report)
        self.assertIn("[PASS] 01_valid/", report)
        self.assertIn("PASS=1, FAIL=0, ERROR=1", report)

    def test_bad_input_is_reported_before_verification(self):
        folder = self.folder / "bad_input"
        folder.mkdir()
        task = copy.deepcopy(next(task for task in run.load_tasks(run.ROOT / "tasks")
                                  if task["predicates"] == ["a", "b"]))
        task["tests"][0]["prefix"][0].pop("b")
        (folder / "incomplete.json").write_text(json.dumps(task), encoding="utf-8")
        process, report = self.cli("bad_input", "--tasks", str(folder))
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn("каждый элемент prefix должен содержать", report)
        self.assertIn("0 тестов", report)

    def test_missing_compiler_is_not_a_successful_negative_test(self):
        process, report = self.cli(
            "missing_compiler", "--test", "*both_reset/late_reset", "--cc",
            str(self.folder / "nonexistent-gcc.exe"))
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn("[ERROR]", report)
        self.assertNotIn("[PASS]", report)

    def test_missing_loop_adds_zeros_after_last_true(self):
        process, report = self.cli("zero_tail", "--test", "*marriage_twice/true_tail")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("Получено: false", report)
        self.assertIn("t=1: q=1 (prefix[1])", report)
        self.assertIn("t=2: q=0 (default_zero)", report)
        self.assertEqual(len(re.findall(r"t=\d+: q=1", report)), 1)
        self.assertIn("loop (по умолчанию): (q=0)", report)

    def test_missing_loop_resets_all_predicates_in_one_step(self):
        process, report = self.cli("zero_pair", "--test", "*both_reset/default_reset")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("Получено: true", report)
        self.assertIn("loop (по умолчанию): (a=0, b=0)", report)

    def test_timeout_leaves_a_log(self):
        with self.assertRaisesRegex(RuntimeError, "Тайм-аут"):
            run.run_command([sys.executable, "-c", "import time; time.sleep(10)"],
                            self.folder, "timeout.log", timeout=0.05)
        self.assertIn("TIMEOUT", (self.folder / "timeout.log").read_text(encoding="utf-8"))

    def test_incomplete_search_and_unrelated_errors_are_not_proofs(self):
        header = "Full statespace search for:\nnever claim + (check)\nacceptance cycles +\n"
        samples = [
            (header + "errors: 0\nWarning: Search not completed", False),
            (header + "pan:1: invalid end state\nerrors: 1", True),
            (header + "pan:1: acceptance cycle\nerrors: 1", False),
            ("+ Partial Order Reduction\n" + header + "errors: 0", False),
            (header + "pan:1: depth limit reached\nerrors: 1", True),
        ]
        for output, has_trail in samples:
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                run.parse_result(output, 0, has_trail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
