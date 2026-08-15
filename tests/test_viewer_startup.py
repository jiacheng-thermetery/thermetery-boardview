# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for src/viewer.py's startup flow.

The viewer used to parse the board *before* any window existed: an
encrypted ASUS .fz could then only report its missing key to stderr and
exit(2), which in a windowed build looks like the app dying on launch.
Startup now builds the window first, paints it, and loads through the
same `_open_board_path` the File -> Open menu uses, so the key prompt
has a live parent to attach to.

These tests substitute a stand-in for ViewerApp, so no tk widget is ever
instantiated and nothing here needs a display or a sample board file —
the suite stays headless-CI safe (Ubuntu runners have tkinter installed
but no X server)."""

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import viewer
from src.parsers.boardview import BoardModel, FZKeyError


class _FakeStatus:
    def __init__(self):
        self.texts = []

    def config(self, **kw):
        if "text" in kw:
            self.texts.append(kw["text"])


class _FakeApp:
    """Records what main() asks the window to do."""

    def __init__(self, board=None, board_path=None):
        self.board = board
        self.board_path = board_path
        self.idle_callbacks = []
        self.opened = []
        self.picker_keys = []
        self.entered_mainloop = False
        self.updates = 0
        self.status = _FakeStatus()
        self.status_refreshes = 0
        self.updates_when_idle_ran = None
        self.open_error = None

    # --- tk surface main() touches -------------------------------------
    def update(self):
        self.updates += 1

    def update_idletasks(self):
        pass

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)

    def mainloop(self):
        self.entered_mainloop = True

    # --- ViewerApp surface ---------------------------------------------
    def _update_status(self):
        self.status_refreshes += 1

    def _open_board_path(self, path, key=None):
        self.opened.append((path, key))
        if self.open_error is not None:
            raise self.open_error

    def _menu_open_board(self, key=None):
        self.picker_keys.append(key)

    # --- test helper ----------------------------------------------------
    def run_idle(self):
        self.updates_when_idle_ran = self.updates
        for callback in list(self.idle_callbacks):
            callback()


def _run_main(argv_tail, parse_board=None, on_app=None):
    """Run viewer.main() with a stand-in ViewerApp; return the instance.

    `parse_board` replaces src.viewer.parse_board — the default sentinel
    fails the test if the interactive path parses anything itself.
    `on_app` is called with the fake as soon as it is built, so a test can
    arm a failure before main() schedules the deferred load.
    """
    created = {}

    def factory(board=None, board_path=None):
        app = _FakeApp(board, board_path)
        created["app"] = app
        if on_app is not None:
            on_app(app)
        return app

    def _must_not_parse(*args, **kwargs):
        raise AssertionError(
            "main() parsed the board before the window existed")

    with mock.patch.object(viewer, "ViewerApp", factory), \
            mock.patch.object(viewer, "check_native_dlls", lambda *a, **k: None), \
            mock.patch.object(viewer, "parse_board",
                              parse_board or _must_not_parse), \
            mock.patch.object(sys, "argv", ["viewer"] + argv_tail):
        viewer.main()
    return created.get("app")


class InteractiveStartupTests(unittest.TestCase):
    def test_window_opens_before_any_board_is_chosen(self):
        app = _run_main([])
        self.assertIsNotNone(app)
        self.assertIsNone(app.board, "the window must come up with no board")
        self.assertIsNone(app.board_path)
        self.assertTrue(app.entered_mainloop)

    def test_window_is_painted_before_the_deferred_load_runs(self):
        """after_idle alone runs before tk has mapped the children or drawn
        a frame, so the load would happen behind a blank window."""
        app = _run_main([])
        self.assertGreaterEqual(app.updates, 1, "main() must call update()")
        app.run_idle()
        self.assertGreaterEqual(
            app.updates_when_idle_ran, 1,
            "the window must already be painted when the load starts")

    def test_no_argument_launch_shows_the_picker_after_the_window(self):
        app = _run_main([])
        # Deferred, not called inline: the picker must not run before the
        # main window has rendered.
        self.assertEqual(app.picker_keys, [])
        self.assertEqual(len(app.idle_callbacks), 1)
        app.run_idle()
        self.assertEqual(app.picker_keys, [None])
        self.assertEqual(app.opened, [])

    def test_board_argument_loads_through_the_gui_open_path(self):
        app = _run_main(["board.fz"])
        app.run_idle()
        self.assertEqual(app.opened, [(Path("board.fz"), None)])
        self.assertEqual(app.picker_keys, [])

    def test_key_argument_is_forwarded_to_the_gui_open_path(self):
        app = _run_main(["board.fz", "--key", "DEADBEEF"])
        app.run_idle()
        self.assertEqual(app.opened, [(Path("board.fz"), "DEADBEEF")])

    def test_key_argument_reaches_a_board_chosen_from_the_picker(self):
        """--key with no path used to be dropped on the floor: the old
        picker branch applied it, the first rewrite did not."""
        app = _run_main(["--key", "DEADBEEF"])
        app.run_idle()
        self.assertEqual(app.picker_keys, ["DEADBEEF"])

    def test_encrypted_board_does_not_exit_before_prompting(self):
        """The reported bug: an encrypted .fz killed the process instead of
        asking for a key. main() must hand the path to _open_board_path,
        which prompts, rather than parsing and exiting itself."""
        app = _run_main(["encrypted.fz"])
        try:
            app.run_idle()
        except SystemExit as exc:  # pragma: no cover - regression guard
            self.fail(f"startup exited instead of prompting: SystemExit({exc.code})")
        self.assertEqual(app.opened, [(Path("encrypted.fz"), None)])

    def test_progress_is_shown_while_the_board_parses(self):
        app = _run_main(["board.fz"])
        app.run_idle()
        self.assertTrue(any("board.fz" in t for t in app.status.texts),
                        f"expected a loading message, got {app.status.texts}")
        self.assertGreaterEqual(app.status_refreshes, 1,
                                "the loading message must be cleared again")


class StartupFailureTests(unittest.TestCase):
    """An exception inside an after_idle callback never reaches the caller
    of mainloop(), so it would bypass the frozen entry point's fatal-error
    dialog and land only in boardviewer.log."""

    def _run_with_failure(self, exc):
        shown = []

        def arm(app):
            app.open_error = exc

        with mock.patch.object(viewer.messagebox, "showerror",
                               lambda *a, **k: shown.append(a)):
            app = _run_main(["board.fz"], on_app=arm)
            app.run_idle()
        return app, shown

    def test_load_failure_is_reported_to_the_user(self):
        app, shown = self._run_with_failure(RuntimeError("corrupt header"))
        self.assertEqual(len(shown), 1, "the failure must surface as a dialog")
        self.assertIn("corrupt header", " ".join(str(a) for a in shown[0]))

    def test_load_failure_does_not_propagate_or_kill_the_app(self):
        app, _ = self._run_with_failure(RuntimeError("corrupt header"))
        # run_idle() completing without raising is the assertion; the
        # viewer stays up so the user can pick another file.
        self.assertTrue(app.entered_mainloop)
        self.assertGreaterEqual(app.status_refreshes, 1)


class SmokeTestStartupTests(unittest.TestCase):
    """--smoke-test stays CLI-shaped: nobody is there to answer a dialog,
    so a missing key must fail the run rather than block on one."""

    def test_smoke_test_requires_a_board_path(self):
        with self.assertRaises(SystemExit) as caught:
            _run_main(["--smoke-test"])
        self.assertEqual(caught.exception.code, 2)

    def test_smoke_test_exits_2_when_the_key_is_missing(self):
        def _raise(*args, **kwargs):
            raise FZKeyError("needs an FZKey")

        with self.assertRaises(SystemExit) as caught:
            _run_main(["board.fz", "--smoke-test"], parse_board=_raise)
        self.assertEqual(caught.exception.code, 2)


class BackgroundParseTests(unittest.TestCase):
    """_parse_in_background runs the parser on a worker thread so the event
    loop keeps painting. These exercise the unbound method against a stub
    `self`: with an instant parser the worker is already finished by the
    time join() returns, so no progress dialog is built and no tk widget
    is needed."""

    class _Stub:
        # 0 means "never wait" — the join still reaps an instant worker.
        _LOADING_DIALOG_DELAY = 0.0

    def _call(self, fake_parse):
        with mock.patch.object(viewer, "parse_board", fake_parse):
            return viewer.ViewerApp._parse_in_background(
                self._Stub(), Path("board.tvw"), key="K")

    def test_returns_the_parsed_board(self):
        sentinel = BoardModel()
        self.assertIs(self._call(lambda p, key=None: sentinel), sentinel)

    def test_path_and_key_reach_the_parser(self):
        seen = {}

        def fake(path, key=None):
            seen["path"], seen["key"] = path, key
            return BoardModel()

        self._call(fake)
        self.assertEqual(seen, {"path": Path("board.tvw"), "key": "K"})

    def test_fzkeyerror_survives_the_thread_boundary(self):
        """The key prompt keys off the exception TYPE, so it has to be
        re-raised as itself rather than wrapped."""
        def fake(path, key=None):
            raise FZKeyError("needs an FZKey")

        with self.assertRaises(FZKeyError):
            self._call(fake)

    def test_other_errors_are_re_raised(self):
        def fake(path, key=None):
            raise RuntimeError("corrupt header")

        with self.assertRaises(RuntimeError):
            self._call(fake)

    def test_parsing_happens_off_the_main_thread(self):
        where = {}

        def fake(path, key=None):
            where["thread"] = threading.current_thread()
            return BoardModel()

        self._call(fake)
        self.assertIsNot(where["thread"], threading.main_thread(),
                         "the parse must not run on the tk thread")


class EmptyBoardTests(unittest.TestCase):
    def test_viewer_app_board_argument_is_optional(self):
        """Guards the signature the UI-first startup depends on."""
        import inspect

        parameter = inspect.signature(viewer.ViewerApp.__init__).parameters["board"]
        self.assertIs(parameter.default, None)

    def test_untitled_window_title(self):
        self.assertEqual(viewer.ViewerApp._title_for(None), "Boardviewer")

    def test_empty_model_is_usable(self):
        board = BoardModel()
        self.assertEqual(len(board.components), 0)
        self.assertEqual(len(board.signals), 0)
        self.assertEqual(board.nets_for_component("U1"), [])


if __name__ == "__main__":
    unittest.main()
