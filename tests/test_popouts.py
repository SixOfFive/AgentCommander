"""Unit tests for the role-popout system.

Covers the registry, summary formatting, mouse parser, and the
keyboard-input action transitions in app._consume_input_chunk.
"""
from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcommander.tui.popouts import (  # noqa: E402
    PopoutBlock,
    PopoutRegistry,
    _count_visible_lines,
    add_delta,
    begin_block,
    finalize_block,
    get_registry,
    is_popout_role,
    list_block_summaries,
    render_summary_line,
)
from agentcommander.tui.mouse_input import (  # noqa: E402
    parse_mouse_events,
)


class TestIsPopoutRole(unittest.TestCase):
    """Spec: collapse sub-agents only — orchestrator + router stream as
    before. The full Role enum has 19 values; everything that isn't
    orchestrator/router should pop out.
    """

    def test_orchestrator_excluded(self) -> None:
        self.assertFalse(is_popout_role("orchestrator"))
        # Capitalization tolerant.
        self.assertFalse(is_popout_role("Orchestrator"))

    def test_router_excluded(self) -> None:
        self.assertFalse(is_popout_role("router"))

    def test_subagents_included(self) -> None:
        for r in ("researcher", "coder", "reviewer", "planner",
                  "summarizer", "vision", "audio", "image_gen", "architect",
                  "critic", "tester", "debugger", "refactorer", "translator",
                  "data_analyst", "preflight", "postmortem"):
            self.assertTrue(is_popout_role(r), f"{r} should pop out")

    def test_empty_or_none(self) -> None:
        self.assertFalse(is_popout_role(""))
        self.assertFalse(is_popout_role(None))


class TestRegistry(unittest.TestCase):
    """The per-process registry: id allocation, ordering, focus cycle."""

    def test_id_allocation_per_role(self) -> None:
        reg = PopoutRegistry()
        b1 = PopoutBlock(id=reg.next_id_for("researcher"), role="researcher")
        b2 = PopoutBlock(id=reg.next_id_for("researcher"), role="researcher")
        b3 = PopoutBlock(id=reg.next_id_for("coder"), role="coder")
        self.assertEqual(b1.id, "researcher-1")
        self.assertEqual(b2.id, "researcher-2")
        self.assertEqual(b3.id, "coder-1")

    def test_reset_wipes_state(self) -> None:
        reg = PopoutRegistry()
        block = PopoutBlock(id=reg.next_id_for("researcher"), role="researcher")
        reg.register(block)
        self.assertEqual(len(reg.blocks), 1)
        reg.reset()
        self.assertEqual(len(reg.blocks), 0)
        # Counter restarts at 1 after reset
        self.assertEqual(reg.next_id_for("researcher"), "researcher-1")

    def test_focus_cycle(self) -> None:
        reg = PopoutRegistry()
        for role in ("researcher", "coder", "reviewer"):
            b = PopoutBlock(id=reg.next_id_for(role), role=role,
                             status="ok", in_viewport=True)
            reg.register(b)
        # First Tab focuses first block
        self.assertEqual(reg.cycle_focus(+1), "researcher-1")
        self.assertEqual(reg.cycle_focus(+1), "coder-1")
        self.assertEqual(reg.cycle_focus(+1), "reviewer-1")
        # Wraps around
        self.assertEqual(reg.cycle_focus(+1), "researcher-1")
        # Reverse
        self.assertEqual(reg.cycle_focus(-1), "reviewer-1")

    def test_focus_skips_running_blocks(self) -> None:
        reg = PopoutRegistry()
        b1 = PopoutBlock(id="a-1", role="a", status="running",
                          in_viewport=True)
        b2 = PopoutBlock(id="b-1", role="b", status="ok", in_viewport=True)
        reg.register(b1)
        reg.register(b2)
        # First Tab should land on b (a is still streaming)
        self.assertEqual(reg.cycle_focus(+1), "b-1")

    def test_focus_skips_scrolled_off(self) -> None:
        reg = PopoutRegistry()
        b1 = PopoutBlock(id="a-1", role="a", status="ok", in_viewport=False)
        b2 = PopoutBlock(id="b-1", role="b", status="ok", in_viewport=True)
        reg.register(b1)
        reg.register(b2)
        self.assertEqual(reg.cycle_focus(+1), "b-1")

    def test_focus_returns_none_when_no_eligible(self) -> None:
        reg = PopoutRegistry()
        # All running → nothing to focus
        b = PopoutBlock(id="x-1", role="x", status="running")
        reg.register(b)
        self.assertIsNone(reg.cycle_focus(+1))


class TestLifecycle(unittest.TestCase):
    """begin → add_delta → finalize: status, collapse-on-success,
    expanded-on-error.
    """

    def setUp(self) -> None:
        get_registry().reset()

    def test_finalize_ok_collapses_by_default(self) -> None:
        b = begin_block("researcher")
        add_delta(b, "some text")
        finalize_block(b, ok=True, completion_tokens=42, duration_ms=1234)
        self.assertEqual(b.status, "ok")
        self.assertTrue(b.collapsed, "ok role should collapse by default")

    def test_finalize_error_stays_expanded(self) -> None:
        b = begin_block("coder")
        finalize_block(b, ok=False, error="provider timeout")
        self.assertEqual(b.status, "error")
        self.assertFalse(b.collapsed,
                          "errored role must NOT collapse — user needs to see it")
        self.assertEqual(b.error, "provider timeout")

    def test_content_buffered_for_replay(self) -> None:
        b = begin_block("researcher")
        add_delta(b, "alpha\n")
        add_delta(b, "beta")
        self.assertEqual(b.content, "alpha\nbeta")

    def test_token_counts_clamped(self) -> None:
        b = begin_block("researcher")
        finalize_block(b, ok=True, completion_tokens=-50, prompt_tokens=-1)
        # Negatives must clamp to 0 — never display "-50 tok" in summary.
        self.assertEqual(b.completion_tokens, 0)
        self.assertEqual(b.prompt_tokens, 0)


class TestSummaryFormatting(unittest.TestCase):
    def test_collapsed_arrow(self) -> None:
        b = PopoutBlock(id="researcher-1", role="researcher",
                        status="ok", collapsed=True,
                        completion_tokens=2847, duration_ms=12300)
        line = render_summary_line(b)
        self.assertIn("▶", line)
        self.assertIn("researcher-1", line)

    def test_expanded_arrow(self) -> None:
        b = PopoutBlock(id="researcher-1", role="researcher",
                        status="ok", collapsed=False)
        self.assertIn("▼", render_summary_line(b))

    def test_error_summary_truncates(self) -> None:
        long_err = "x" * 200
        b = PopoutBlock(id="x-1", role="x", status="error",
                        error=long_err, collapsed=False)
        line = render_summary_line(b)
        # Should contain the error indicator + truncated error
        self.assertIn("✗", line)
        # The truncated error in the bracket section is at most 60 chars + ellipsis.
        # Full inner: ['0s', '0 tok', '✗ <60 chars>…']
        self.assertNotIn("x" * 100, line)

    def test_duration_formatting(self) -> None:
        from agentcommander.tui.popouts import _fmt_duration
        self.assertEqual(_fmt_duration(0), "0s")
        self.assertEqual(_fmt_duration(500), "500ms")
        self.assertEqual(_fmt_duration(1500), "1.5s")
        self.assertEqual(_fmt_duration(75_000), "1m15s")

    def test_token_formatting(self) -> None:
        from agentcommander.tui.popouts import _fmt_tokens
        self.assertEqual(_fmt_tokens(42), "42 tok")
        self.assertEqual(_fmt_tokens(1234), "1.2k tok")
        self.assertEqual(_fmt_tokens(1_500_000), "1.5M tok")


class TestLineCounting(unittest.TestCase):
    """Wrap-aware line counting drives how many lines we walk back to
    erase on collapse. Conservative on the LOW side (under-count) is the
    safe failure mode — over-counting clobbers content above the block.
    """

    def test_no_newlines_no_wrap(self) -> None:
        # Short text, no newline → 0 rows of standalone "rows" (the cursor
        # sits on the partial line; we don't count that as a separate row).
        self.assertEqual(_count_visible_lines("hello", cols=80, indent_cols=4), 0)

    def test_explicit_newlines(self) -> None:
        self.assertEqual(_count_visible_lines("a\nb", cols=80, indent_cols=4), 1)
        self.assertEqual(_count_visible_lines("a\nb\nc", cols=80, indent_cols=4), 2)

    def test_wrap_induced(self) -> None:
        # cols=20, indent=4 → body=16. 32 chars → 32//16 = 2 wrap rows.
        text = "x" * 32
        self.assertEqual(_count_visible_lines(text, cols=20, indent_cols=4), 2)

    def test_zero_or_negative_cols(self) -> None:
        # Pathological: terminal too narrow. Should fall back to plain
        # newline count without crashing.
        self.assertEqual(_count_visible_lines("a\nb", cols=2, indent_cols=4), 1)


class TestMouseParser(unittest.TestCase):
    def test_single_press(self) -> None:
        rem, evs = parse_mouse_events("\x1b[<0;42;7M")
        self.assertEqual(rem, "")
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev.button, 0)
        self.assertEqual(ev.x, 42)
        self.assertEqual(ev.y, 7)
        self.assertTrue(ev.pressed)

    def test_release(self) -> None:
        _, evs = parse_mouse_events("\x1b[<0;1;1m")
        self.assertEqual(len(evs), 1)
        self.assertFalse(evs[0].pressed)

    def test_mixed_with_typing(self) -> None:
        rem, evs = parse_mouse_events("hello\x1b[<0;5;3Mworld!")
        self.assertEqual(rem, "helloworld!")
        self.assertEqual(len(evs), 1)

    def test_wheel_motion_ignored(self) -> None:
        # bit 5 (32) = motion, bit 6 (64) = wheel — both should drop.
        _, evs = parse_mouse_events("\x1b[<32;1;1M\x1b[<64;1;1M\x1b[<0;5;3M")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].x, 5)

    def test_no_mouse_passthrough(self) -> None:
        rem, evs = parse_mouse_events("plain text")
        self.assertEqual(rem, "plain text")
        self.assertEqual(evs, [])


class TestInputChunkActions(unittest.TestCase):
    """Verify the new keyboard actions out of _consume_input_chunk:
    Tab/Shift-Tab/Space/Enter/Esc on an empty buffer all become popout
    actions; on a typed buffer they continue to work or get dropped as
    before so we don't break the prompt.
    """

    def _consume(self, buf: str, chunk: str):
        from agentcommander.tui.app import _consume_input_chunk
        return _consume_input_chunk(buf, chunk)

    def test_tab_empty_buffer_focuses_next(self) -> None:
        new, action = self._consume("", "\t")
        self.assertEqual(new, "")
        self.assertEqual(action, ("popout_focus_next", ""))

    def test_tab_with_typing_dropped(self) -> None:
        new, action = self._consume("hello", "\t")
        self.assertEqual(new, "hello")
        self.assertIsNone(action)

    def test_shift_tab_empty_buffer_focuses_prev(self) -> None:
        new, action = self._consume("", "\x1b[Z")
        self.assertEqual(action, ("popout_focus_prev", ""))

    def test_space_empty_buffer_toggles(self) -> None:
        new, action = self._consume("", " ")
        self.assertEqual(action, ("popout_toggle", ""))

    def test_space_with_typing_appends(self) -> None:
        new, action = self._consume("foo", " ")
        self.assertEqual(new, "foo ")
        self.assertIsNone(action)

    def test_enter_empty_toggles(self) -> None:
        new, action = self._consume("", "\r")
        self.assertEqual(action, ("popout_toggle", ""))

    def test_enter_with_typing_submits(self) -> None:
        new, action = self._consume("/help", "\n")
        self.assertEqual(action, ("submit", "/help"))

    def test_esc_empty_buffer_blurs(self) -> None:
        new, action = self._consume("", "\x1b")
        self.assertEqual(action, ("popout_blur", ""))


class TestRenderStateReset(unittest.TestCase):
    """Round 15 regression: pipeline aborts mid-role (Ctrl-C, /stop,
    crash) leave ``_streaming_state`` pointing at an orphaned block.
    The next turn's registry ``reset()`` only wipes the popout registry —
    render-side state needs its own reset so the same role on the next
    turn opens a fresh block instead of streaming into the dead one.
    """

    def setUp(self) -> None:
        get_registry().reset()
        # Silence ANSI output during the test.
        self._stdout = sys.stdout
        sys.stdout = StringIO()

    def tearDown(self) -> None:
        sys.stdout = self._stdout
        from agentcommander.tui.render import reset_render_state
        reset_render_state()
        get_registry().reset()

    def test_reset_render_state_clears_dangling_block(self) -> None:
        from agentcommander.tui.render import (
            _streaming_state, render_role_delta, reset_render_state,
        )
        # Run #1: researcher streams, then crash (no role/end fires).
        render_role_delta("researcher", "starting...")
        self.assertEqual(_streaming_state["role"], "researcher")
        self.assertIsNotNone(_streaming_state["block"])

        # Run #2 begins — app.py calls registry.reset() AND
        # reset_render_state() at the start of every pipeline run.
        get_registry().reset()
        reset_render_state()

        # First delta of run #2 with the SAME role name. Without the fix,
        # render_role_delta would skip the "new role" branch (state still
        # says "researcher") and the new turn would accumulate into the
        # dead block. With the fix, a fresh block opens.
        render_role_delta("researcher", "fresh start")
        new_blocks = list(get_registry().blocks)
        self.assertEqual(len(new_blocks), 1,
                          "new turn must create a fresh block")
        self.assertEqual(new_blocks[0].id, "researcher-1")

    def test_reset_render_state_clears_pending_role_usage(self) -> None:
        from agentcommander.tui.render import (
            _pending_role_usage, note_role_end_for_popout,
            reset_render_state,
        )
        note_role_end_for_popout("researcher", prompt_tokens=10,
                                  completion_tokens=20)
        note_role_end_for_popout("coder", prompt_tokens=5,
                                  completion_tokens=15)
        self.assertEqual(set(_pending_role_usage.keys()),
                          {"researcher", "coder"})
        reset_render_state()
        self.assertEqual(_pending_role_usage, {})


class TestSlashCommand(unittest.TestCase):
    """The /popout command dispatches against the registry."""

    def setUp(self) -> None:
        get_registry().reset()

    def test_command_registered(self) -> None:
        from agentcommander.tui.commands import COMMANDS
        self.assertIn("/popout", COMMANDS)
        self.assertIn("/po", COMMANDS)
        # Aliases point to the same command
        self.assertIs(COMMANDS["/popout"], COMMANDS["/po"])

    def test_list_block_summaries_shape(self) -> None:
        b = begin_block("researcher")
        finalize_block(b, ok=True, completion_tokens=100, duration_ms=2000)
        rows = list_block_summaries()
        self.assertEqual(len(rows), 1)
        bid, role, status, detail = rows[0]
        self.assertEqual(bid, "researcher-1")
        self.assertEqual(role, "researcher")
        self.assertEqual(status, "ok")
        self.assertIn("collapsed", detail)


if __name__ == "__main__":
    unittest.main()
