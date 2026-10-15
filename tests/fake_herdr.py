"""A fake `herdr` CLI for Herdr's runner: answers calls from a table and records them."""

import json
import subprocess

from ticketmaster.herdr import HerdrError


class FakeHerdr:
    """Answers `herdr` calls from a {(group, verb): result | callable} table and records argv."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv[1:])
        answer = self.answers.get(tuple(argv[1:3]), {})
        if callable(answer):
            answer = answer(argv[1:])
        if isinstance(answer, HerdrError):
            err = json.dumps({"error": {"code": str(answer), "message": "x"}})
            return subprocess.CompletedProcess(argv, 1, "", err)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": answer}), "")

    def verbs(self):
        return [tuple(c[:2]) for c in self.calls]

    def first(self, group, verb):
        return next(c for c in self.calls if c[:2] == [group, verb])
