"""A tiny in-process stand-in for the Durable Functions runtime.

It drives orchestrator generators: activities run synchronously, timers advance a fake
clock instantly, sub-orchestrators run recursively and ``task_all`` resolves each task.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class FakeContext:
    def __init__(self, input_: Any, clock: dict):
        self._input = input_
        self._clock = clock  # shared {"now": datetime} so sub-orchestrators see the same time

    def get_input(self) -> Any:
        return self._input

    @property
    def current_utc_datetime(self) -> datetime:
        return self._clock["now"]

    def call_activity(self, name: str, input_: Any):
        return ("activity", name, input_)

    def call_sub_orchestrator(self, name: str, input_: Any):
        return ("sub", name, input_)

    def create_timer(self, fire_at: datetime):
        return ("timer", fire_at)

    def task_all(self, tasks: list):
        return ("all", tasks)


class Runtime:
    def __init__(
        self,
        activities: dict[str, Callable[[Any], Any]],
        orchestrators: dict[str, Callable],
        start: datetime | None = None,
    ):
        self.activities = activities
        self.orchestrators = orchestrators
        self.clock = {"now": start or datetime(2026, 1, 1, 6, 0, tzinfo=UTC)}
        self.calls: list[tuple] = []
        self.timers: list[datetime] = []

    def run(self, name: str, input_: Any) -> Any:
        gen = self.orchestrators[name](FakeContext(input_, self.clock))
        value, exc = None, None
        while True:
            try:
                task = gen.throw(exc) if exc else gen.send(value)
            except StopIteration as stop:
                return stop.value
            value, exc = self._resolve(task)

    def _resolve(self, task) -> tuple[Any, BaseException | None]:
        kind = task[0]
        if kind == "activity":
            _, name, input_ = task
            self.calls.append((name, input_))
            try:
                return self.activities[name](input_), None
            except Exception as e:
                return None, e
        if kind == "timer":
            fire_at = task[1]
            assert fire_at >= self.clock["now"], "timer scheduled in the past"
            self.timers.append(fire_at)
            self.clock["now"] = fire_at
            return None, None
        if kind == "sub":
            _, name, input_ = task
            return self.run(name, input_), None
        if kind == "all":
            out = []
            for t in task[1]:
                value, exc = self._resolve(t)
                if exc:
                    return None, exc
                out.append(value)
            return out, None
        raise AssertionError(f"unknown task {task!r}")
