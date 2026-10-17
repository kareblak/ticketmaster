"""A fake command runner for the plain backend: answers argv from a table, by its longest matching prefix, and
records each call."""

import json
import subprocess
from dataclasses import dataclass


@dataclass
class Call:
    argv: list[str]
    cwd: str | None
    env: dict | None
    detach: bool


def ok(stdout="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(returncode=1, stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, "", stderr)


def agents(*entries) -> subprocess.CompletedProcess:
    return ok(json.dumps(list(entries)))


class FakeRunner:
    """`answers` maps an argv prefix (a tuple) to a CompletedProcess, an exception to raise, or a callable of argv
    returning either. Unanswered calls succeed with no output."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls: list[Call] = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=30.0, detach=False):
        self.calls.append(Call(list(argv), None if cwd is None else str(cwd), None if env is None else dict(env),
                               detach))
        answer = next((self.answers[tuple(argv[:n])] for n in range(len(argv), 0, -1)
                       if tuple(argv[:n]) in self.answers), ok())
        if callable(answer):
            answer = answer(argv)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def argvs(self):
        return [c.argv for c in self.calls]

    def last(self, program):
        return next(c for c in reversed(self.calls) if c.argv[0] == program)
