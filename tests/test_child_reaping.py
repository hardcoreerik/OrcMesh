"""Tests for tracking spawned tools and reaping them at exit.

The failure these exist for: OrcMesh goes away without running its stop path, the
child tool keeps the RTL-SDR open, and the next launch reports a busy dongle with
nothing running and nothing for the user to stop. A dongle held by a process
nobody can see is the worst version of a busy device.
"""
from __future__ import annotations

import subprocess

import pytest

from meshchat.services import rtl_tools


class _Child:
    """A Popen double with the handful of members the registry touches."""

    def __init__(
        self,
        pid: int,
        *,
        alive: bool = True,
        ignores_terminate: bool = False,
        terminate_raises: bool = False,
    ) -> None:
        self.pid = pid
        self._alive = alive
        self._ignores_terminate = ignores_terminate
        self._terminate_raises = terminate_raises
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        if self._terminate_raises:
            raise OSError("no such process")
        self.terminated = True
        if not self._ignores_terminate:
            self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired("tool", timeout)
        return 0


@pytest.fixture(autouse=True)
def _empty_registry():
    """The registry is process-wide state, so no test may inherit another's.

    Reaches into the module's privates deliberately: the point is to control the
    global, not to exercise a public seam that does not exist.
    """
    def clear() -> None:
        with rtl_tools._children_lock:
            rtl_tools._children.clear()

    clear()
    yield
    clear()


class TestTracking:
    def test_spawn_remembers_the_child(self, monkeypatch):
        child = _Child(101)
        monkeypatch.setattr(rtl_tools.subprocess, "Popen", lambda *a, **k: child)

        spawned = rtl_tools.spawn(["rtl_sdr", "-"], label="the spectrum view")

        assert spawned is child
        assert rtl_tools.tracked_children() == ["the spectrum view"]

    def test_spawn_hands_the_tool_strings_even_when_given_paths(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            rtl_tools.subprocess, "Popen",
            lambda args, **k: seen.update(args=args) or _Child(102),
        )

        rtl_tools.spawn([rtl_tools.Path("C:/tools/rtl_sdr.exe"), "-f", "915M"], label="x")

        assert seen["args"] == ["C:\\tools\\rtl_sdr.exe", "-f", "915M"]

    def test_the_same_child_tracked_twice_is_tracked_once(self):
        child = _Child(103)

        rtl_tools.track_child(child, "a")
        rtl_tools.track_child(child, "b")

        assert len(rtl_tools.tracked_children()) == 1

    def test_untracking_forgets_it(self):
        child = _Child(104)
        rtl_tools.track_child(child, "the spectrum view")

        rtl_tools.untrack_child(child)

        assert rtl_tools.tracked_children() == []

    def test_untracking_none_is_not_an_error(self):
        """The workers call this with whatever `self._proc` held, which can be None."""
        rtl_tools.untrack_child(None)

        assert rtl_tools.tracked_children() == []

    def test_a_child_that_was_closed_cleanly_is_not_reaped_again(self):
        """Untracking on close is what keeps a normal stop out of the exit path."""
        child = _Child(105)
        rtl_tools.track_child(child, "the spectrum view")
        rtl_tools.untrack_child(child)

        assert rtl_tools.terminate_children() == 0
        assert not child.terminated

    def test_the_exit_hook_is_registered_once_however_many_children(self, monkeypatch):
        registered: list[object] = []
        monkeypatch.setattr(rtl_tools.atexit, "register", registered.append)
        monkeypatch.setattr(rtl_tools, "_atexit_hooked", False)

        rtl_tools.track_child(_Child(106), "a")
        rtl_tools.track_child(_Child(107), "b")

        assert registered == [rtl_tools.terminate_children]


class TestReaping:
    def test_a_running_child_is_terminated_and_counted(self):
        child = _Child(201)
        rtl_tools.track_child(child, "the spectrum view")

        assert rtl_tools.terminate_children() == 1
        assert child.terminated

    def test_a_child_that_already_exited_is_left_alone(self):
        """Otherwise every clean exit would report a kill that never happened."""
        child = _Child(202, alive=False)
        rtl_tools.track_child(child, "the spectrum view")

        assert rtl_tools.terminate_children() == 0
        assert not child.terminated

    def test_a_child_that_ignores_terminate_is_killed(self):
        """A wedged driver is exactly the case this exists for."""
        child = _Child(203, ignores_terminate=True)
        rtl_tools.track_child(child, "the spectrum view")

        assert rtl_tools.terminate_children() == 1
        assert child.killed

    def test_the_registry_is_emptied_even_when_a_child_resists(self):
        stubborn = _Child(204, ignores_terminate=True)
        rtl_tools.track_child(stubborn, "stubborn")

        rtl_tools.terminate_children()

        assert rtl_tools.tracked_children() == []

    def test_a_child_that_cannot_be_signalled_does_not_raise(self):
        """A process that died between the poll and the signal is the common case."""
        gone = _Child(205, terminate_raises=True)
        rtl_tools.track_child(gone, "the spectrum view")

        assert rtl_tools.terminate_children() == 0

    def test_every_tracked_child_is_reaped_not_just_the_first(self):
        children = [_Child(206), _Child(207), _Child(208)]
        for index, child in enumerate(children):
            rtl_tools.track_child(child, f"worker {index}")

        assert rtl_tools.terminate_children() == 3
        assert all(child.terminated for child in children)

    def test_reaping_with_nothing_tracked_is_a_no_op(self):
        assert rtl_tools.terminate_children() == 0
