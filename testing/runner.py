import contextlib

from hecate import Runner


class PrintsErrorRunner(Runner):
    def __init__(self, *args, **kwargs):
        self._prev_screenshot = None
        super().__init__(*args, **kwargs)

    def screenshot(self, *args, **kwargs):
        ret = super().screenshot(*args, **kwargs)
        if ret != self._prev_screenshot:
            print('=' * 79, flush=True)
            print(ret, end='', flush=True)
            print('=' * 79, flush=True)
            self._prev_screenshot = ret
        return ret

    def await_text(self, text, timeout=None):
        """copied from the base implementation but doesn't munge newlines"""
        for _ in self.poll_until_timeout(timeout):
            screen = self.screenshot()
            if text in screen:  # pragma: no branch
                return
        raise AssertionError(
            f'Timeout while waiting for text {text!r} to appear',
        )

    def await_text_missing(self, s):
        """largely based on await_text"""
        for _ in self.poll_until_timeout():
            screen = self.screenshot()
            munged = screen.replace('\n', '')
            if s not in munged:  # pragma: no branch
                return
        raise AssertionError(
            f'Timeout while waiting for text {s!r} to disappear',
        )

    def assert_cursor_line_equals(self, s):
        cursor_line = self._get_cursor_line()
        assert cursor_line == s, (cursor_line, s)

    def assert_screen_line_equals(self, n, s):
        screen_line = self._get_screen_line(n)
        assert screen_line == s, (screen_line, s)

    def assert_full_contents(self, s):
        contents = self.screenshot()
        assert contents == s

    def get_pane_size(self):
        cmd = ('display', '-t0', '-p', '#{pane_width}\t#{pane_height}')
        w, h = self.tmux.execute_command(*cmd).split()
        return int(w), int(h)

    def _get_cursor_position(self):
        cmd = ('display', '-t0', '-p', '#{cursor_x}\t#{cursor_y}')
        x, y = self.tmux.execute_command(*cmd).split()
        return int(x), int(y)

    def await_cursor_position(self, *, x, y):
        for _ in self.poll_until_timeout():
            pos = self._get_cursor_position()
            if pos == (x, y):  # pragma: no branch
                return

        raise AssertionError(
            f'Timeout while waiting for cursor to reach {(x, y)}\n'
            f'Last cursor position: {pos}',
        )

    def _get_screen_line(self, n):
        return self.screenshot().splitlines()[n]

    def _get_cursor_line(self):
        _, y = self._get_cursor_position()
        return self._get_screen_line(y)

    @contextlib.contextmanager
    def resize(self, width, height):
        current_w, current_h = self.get_pane_size()
        sleep_cmd = (
            'bash', '-c',
            f'echo {"*" * (current_w * current_h)} && '
            f'exec sleep infinity',
        )

        panes = 0

        hsplit_w = current_w - width - 1
        if hsplit_w > 0:
            cmd = ('split-window', '-ht0', '-l', hsplit_w, *sleep_cmd)
            self.tmux.execute_command(*cmd)
            panes += 1

        vsplit_h = current_h - height - 1
        if vsplit_h > 0:  # pragma: no branch  # TODO
            cmd = ('split-window', '-vt0', '-l', vsplit_h, *sleep_cmd)
            self.tmux.execute_command(*cmd)
            panes += 1

        assert self.get_pane_size() == (width, height)
        try:
            yield
        finally:
            for _ in range(panes):
                self.tmux.execute_command('kill-pane', '-t1')

    def press_and_enter(self, s):
        self.press(s)
        self.press('Enter')

    def answer_no_if_modified(self):
        if '*' in self._get_screen_line(0):
            self.press('n')

    def run(self, callback):
        # this is a bit of a hack, the in-process fake defers all execution
        callback()


@contextlib.contextmanager
def and_exit(h):
    yield
    # only try and exit in non-exceptional cases
    h.press('^X')
    h.answer_no_if_modified()
    h.await_exit()


def trigger_command_mode(h):
    # in order to enter a steady state, trigger an unknown key first and then
    # press escape to open the command mode.  this is necessary as `Escape` is
    # the start of "escape sequences" and sending characters too quickly will
    # be interpreted as a single keypress
    h.press('^J')
    h.await_text('unknown key')
    h.press('Escape')
    h.await_text_missing('unknown key')
