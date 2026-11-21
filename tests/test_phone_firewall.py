"""Whether Windows keeps the phones out (app/services/phone/firewall.py): Windows' answer read and
matched against this Python. PowerShell is never started here: a fake `run` answers."""

from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from collections.abc import Sequence
from typing import Any
from unittest import mock

from app.services.phone import firewall
from app.services.phone.firewall import NetworkFacts, check_network, read_facts, this_python

PYTHON = r"C:\Users\Alex\AppData\Local\Programs\Python\Python313\pythonw.exe"
OTHER = r"C:\Program Files\Some Game\game.exe"


def rule(program: str, profile: str = "Public") -> dict[str, str]:
    return {"program": program, "profile": profile}


class ReadingTests(unittest.TestCase):
    def facts(self, category: str, *rules: Any) -> NetworkFacts | None:
        return read_facts({"category": category, "blocked": list(rules)}, [PYTHON])

    def blocked(self, category: str, *rules: Any) -> bool:
        facts = self.facts(category, *rules)
        assert facts is not None
        return facts.blocked

    def test_a_public_network(self) -> None:
        self.assertEqual(self.facts("Public"), NetworkFacts(public=True, blocked=False))
        self.assertEqual(self.facts("Private"), NetworkFacts(public=False, blocked=False))
        self.assertEqual(
            self.facts("DomainAuthenticated"), NetworkFacts(public=False, blocked=False)
        )

    def test_a_block_rule_for_this_python_on_the_network_in_use(self) -> None:
        # What Cancel on Windows' question leaves: a block rule for that network's profile.
        self.assertTrue(self.blocked("Public", rule(PYTHON, "Public")))
        self.assertTrue(self.blocked("Private", rule(PYTHON, "Any")))
        self.assertTrue(self.blocked("Private", rule(PYTHON, "Private, Public")))
        self.assertTrue(self.blocked("DomainAuthenticated", rule(PYTHON, "Domain")))
        # Another network's rule doesn't count; nor another program's.
        self.assertFalse(self.blocked("Private", rule(PYTHON, "Public")))
        self.assertFalse(self.blocked("Public", rule(OTHER, "Any")))
        # Not knowing the network, any rule for it counts.
        self.assertTrue(self.blocked("", rule(PYTHON, "Public")))

    def test_paths_compare_as_windows_does(self) -> None:
        shouting = PYTHON.upper()
        self.assertTrue(self.blocked("Public", rule(f'"{shouting}"')))
        with mock.patch.dict(os.environ, {"AG_TEST_HOME": r"C:\Users\Alex"}):
            spelled = r"%AG_TEST_HOME%\AppData\Local\Programs\Python\Python313\pythonw.exe"
            self.assertTrue(self.blocked("Public", rule(spelled)))

    def test_one_rule_alone_and_odd_answers(self) -> None:
        one = read_facts({"category": "Public", "blocked": rule(PYTHON)}, [PYTHON])
        self.assertEqual(one, NetworkFacts(public=True, blocked=True))
        self.assertEqual(
            read_facts({"category": "Private", "blocked": None}, [PYTHON]),
            NetworkFacts(public=False, blocked=False),
        )
        self.assertEqual(
            self.facts("Private", "not a rule", {"profile": "Any"}, rule("")),
            NetworkFacts(public=False, blocked=False),
        )
        self.assertIsNone(read_facts([], [PYTHON]))
        self.assertIsNone(read_facts({"category": "Public", "blocked": 3}, [PYTHON]))

    def test_this_python_has_both_programs(self) -> None:
        names = {os.path.basename(one) for one in this_python()}
        self.assertLessEqual({"python.exe", "pythonw.exe"}, names)


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_answer_is_read(self) -> None:
        asked: list[tuple[list[str], float]] = []

        async def run(args: Sequence[str], timeout_s: float) -> bytes | None:
            asked.append((list(args), timeout_s))
            answer = {"category": "Public", "blocked": [rule(PYTHON, "Public")]}
            return b"\xef\xbb\xbf" + json.dumps(answer).encode()

        with self.assertLogs(firewall.log, logging.INFO) as logs:
            facts = await check_network(run=run, programs=[PYTHON])
        self.assertEqual(facts, NetworkFacts(public=True, blocked=True))
        args, timeout_s = asked[0]
        self.assertEqual(args[0], "powershell.exe")
        self.assertIn("-NonInteractive", args)
        self.assertIn("-EncodedCommand", args)
        self.assertLessEqual(timeout_s, 30)
        self.assertNotIn("Alex", "\n".join(logs.output))  # no path in the log

    async def test_no_answer_says_nothing(self) -> None:
        for out in (None, b"", b"not json", b"[]"):
            with self.subTest(out=out):

                async def run(args: Sequence[str], timeout_s: float) -> bytes | None:
                    return out

                self.assertIsNone(await check_network(run=run, programs=[PYTHON]))

    async def test_a_program_that_doesnt_end_in_time_is_killed(self) -> None:
        with mock.patch.object(firewall, "_NO_WINDOW", 0):
            out = await firewall._run(
                [sys.executable, "-c", "import time; time.sleep(30)"], 0.5
            )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
