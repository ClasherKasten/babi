import argparse
import collections
import contextlib
import curses
import enum
import io
import os
import signal
from typing import Dict
from typing import Generator
from typing import IO
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Tuple
from typing import Union

VERSION_STR = 'babi v0'


class Margin(NamedTuple):
    header: bool
    footer: bool

    @property
    def body_lines(self) -> int:
        return curses.LINES - self.header - self.footer

    @property
    def page_size(self) -> int:
        if self.body_lines <= 2:
            return 1
        else:
            return self.body_lines - 2

    @classmethod
    def from_screen(cls, screen: 'curses._CursesWindow') -> 'Margin':
        if curses.LINES == 1:
            return cls(header=False, footer=False)
        elif curses.LINES == 2:
            return cls(header=False, footer=True)
        else:
            return cls(header=True, footer=True)


class Position:
    def __init__(self) -> None:
        self.file_line = self.cursor_line = self.x = self.x_hint = 0

    def __repr__(self) -> str:
        attrs = ', '.join(f'{k}={v}' for k, v in self.__dict__.items())
        return f'{type(self).__name__}({attrs})'

    def _scroll_amount(self) -> int:
        return int(curses.LINES / 2 + .5)

    def _set_x_after_vertical_movement(self, lines: List[str]) -> None:
        self.x = min(len(lines[self.cursor_line]), self.x_hint)

    def maybe_scroll_down(self, margin: Margin) -> None:
        if self.cursor_line >= self.file_line + margin.body_lines:
            self.file_line += self._scroll_amount()

    def down(self, margin: Margin, lines: List[str]) -> None:
        if self.cursor_line < len(lines) - 1:
            self.cursor_line += 1
            self.maybe_scroll_down(margin)
            self._set_x_after_vertical_movement(lines)

    def maybe_scroll_up(self, margin: Margin) -> None:
        if self.cursor_line < self.file_line:
            self.file_line -= self._scroll_amount()
            self.file_line = max(self.file_line, 0)

    def up(self, margin: Margin, lines: List[str]) -> None:
        if self.cursor_line > 0:
            self.cursor_line -= 1
            self.maybe_scroll_up(margin)
            self._set_x_after_vertical_movement(lines)

    def right(self, margin: Margin, lines: List[str]) -> None:
        if self.x >= len(lines[self.cursor_line]):
            if self.cursor_line < len(lines) - 1:
                self.x = 0
                self.cursor_line += 1
                self.maybe_scroll_down(margin)
        else:
            self.x += 1
        self.x_hint = self.x

    def left(self, margin: Margin, lines: List[str]) -> None:
        if self.x == 0:
            if self.cursor_line > 0:
                self.cursor_line -= 1
                self.x = len(lines[self.cursor_line])
                self.maybe_scroll_up(margin)
        else:
            self.x -= 1
        self.x_hint = self.x

    def home(self, margin: Margin, lines: List[str]) -> None:
        self.x = self.x_hint = 0

    def end(self, margin: Margin, lines: List[str]) -> None:
        self.x = self.x_hint = len(lines[self.cursor_line])

    def ctrl_home(self, margin: Margin, lines: List[str]) -> None:
        self.x = self.x_hint = 0
        self.cursor_line = self.file_line = 0

    def ctrl_end(self, margin: Margin, lines: List[str]) -> None:
        self.x = self.x_hint = 0
        self.cursor_line = len(lines) - 1
        if self.file_line < self.cursor_line - margin.body_lines:
            self.file_line = self.cursor_line - margin.body_lines * 3 // 4 + 1

    def page_up(self, margin: Margin, lines: List[str]) -> None:
        if self.cursor_line < margin.body_lines:
            self.cursor_line = self.file_line = 0
        else:
            pos = self.file_line - margin.page_size
            self.cursor_line = self.file_line = pos
        self._set_x_after_vertical_movement(lines)

    def page_down(self, margin: Margin, lines: List[str]) -> None:
        if self.file_line + margin.body_lines >= len(lines):
            self.cursor_line = len(lines) - 1
        else:
            pos = self.file_line + margin.page_size
            self.cursor_line = self.file_line = pos
        self._set_x_after_vertical_movement(lines)

    DISPATCH = {
        curses.KEY_DOWN: down,
        curses.KEY_UP: up,
        curses.KEY_LEFT: left,
        curses.KEY_RIGHT: right,
        curses.KEY_HOME: home,
        curses.KEY_END: end,
        curses.KEY_PPAGE: page_up,
        curses.KEY_NPAGE: page_down,
    }
    DISPATCH_KEY = {
        b'^A': home,
        b'^E': end,
        b'^Y': page_up,
        b'^V': page_down,
        b'kHOM5': ctrl_home,
        b'kEND5': ctrl_end,
    }

    def cursor_y(self, margin: Margin) -> int:
        return self.cursor_line - self.file_line + margin.header

    def line_x(self) -> int:
        margin = min(curses.COLS - 3, 6)
        if self.x + 1 < curses.COLS:
            return 0
        elif curses.COLS == 1:
            return self.x
        else:
            return (
                curses.COLS - margin - 2 +
                (self.x + 1 - curses.COLS) //
                (curses.COLS - margin - 2) *
                (curses.COLS - margin - 2)
            )

    def cursor_x(self) -> int:
        return self.x - self.line_x()

    def move_cursor(
            self,
            stdscr: 'curses._CursesWindow',
            margin: Margin,
    ) -> None:
        stdscr.move(self.cursor_y(margin), self.cursor_x())


def _get_color_pair_mapping() -> Dict[Tuple[int, int], int]:
    ret = {}
    i = 0
    for bg in range(-1, 16):
        for fg in range(bg, 16):
            ret[(fg, bg)] = i
            i += 1
    return ret


COLORS = _get_color_pair_mapping()
del _get_color_pair_mapping


def _has_colors() -> bool:
    return curses.has_colors and curses.COLORS >= 16


def _color(fg: int, bg: int) -> int:
    if _has_colors():
        if bg > fg:
            return curses.A_REVERSE | curses.color_pair(COLORS[(bg, fg)])
        else:
            return curses.color_pair(COLORS[(fg, bg)])
    else:
        if bg > fg:
            return curses.A_REVERSE | curses.color_pair(0)
        else:
            return curses.color_pair(0)


def _init_colors(stdscr: 'curses._CursesWindow') -> None:
    curses.use_default_colors()
    if not _has_colors():
        return
    for (fg, bg), pair in COLORS.items():
        if pair == 0:  # cannot reset pair 0
            continue
        curses.init_pair(pair, fg, bg)


class Header:
    def __init__(self, file: 'File', idx: int, n_files: int) -> None:
        self.file = file
        self.idx = idx
        self.n_files = n_files

    def draw(self, stdscr: 'curses._CursesWindow') -> None:
        filename = self.file.filename or '<<new file>>'
        if self.file.modified:
            filename += ' *'
        if self.n_files > 1:
            files = f'[{self.idx + 1}/{self.n_files}] '
            version_width = len(VERSION_STR) + 2 + len(files)
        else:
            files = ''
            version_width = len(VERSION_STR) + 2
        centered = filename.center(curses.COLS)[version_width:]
        s = f' {VERSION_STR} {files}{centered}{files}'
        stdscr.insstr(0, 0, s, curses.A_REVERSE)


class Status:
    def __init__(self) -> None:
        self._status = ''
        self._action_counter = -1

    def update(self, status: str, margin: Margin) -> None:
        self._status = status
        # when the window is only 1-tall, hide the status quicker
        if margin.footer:
            self._action_counter = 25
        else:
            self._action_counter = 1

    def draw(self, stdscr: 'curses._CursesWindow', margin: Margin) -> None:
        if margin.footer or self._status:
            stdscr.insstr(curses.LINES - 1, 0, ' ' * curses.COLS)
            if self._status:
                status = f' {self._status} '
                x = (curses.COLS - len(status)) // 2
                if x < 0:
                    x = 0
                    status = status.strip()
                stdscr.insstr(curses.LINES - 1, x, status, curses.A_REVERSE)

    def tick(self) -> None:
        self._action_counter -= 1
        if self._action_counter < 0:
            self._status = ''


class File:
    def __init__(self, filename: Optional[str]) -> None:
        self.filename = filename
        self.modified = False
        self.pos = Position()
        self.lines: List[str] = []
        self.nl = '\n'

    def ensure_loaded(self, status: Status, margin: Margin) -> None:
        if self.lines:
            return

        if self.filename is not None and os.path.isfile(self.filename):
            with open(self.filename, newline='') as f:
                self.lines, self.nl, mixed = _get_lines(f)
        else:
            if self.filename is not None:
                if os.path.lexists(self.filename):
                    status.update(f'{self.filename!r} is not a file', margin)
                    self.filename = None
                else:
                    status.update('(new file)', margin)
            self.lines, self.nl, mixed = _get_lines(io.StringIO(''))

        if mixed:
            status.update(
                f'mixed newlines will be converted to {self.nl!r}', margin,
            )
            self.modified = True

    def backspace(self, margin: Margin) -> None:
        # backspace at the beginning of the file does nothing
        if self.pos.cursor_line == 0 and self.pos.x == 0:
            pass
        # at the beginning of the line, we join the current line and
        # the previous line
        elif self.pos.x == 0:
            victim = self.lines.pop(self.pos.cursor_line)
            new_x = len(self.lines[self.pos.cursor_line - 1])
            self.lines[self.pos.cursor_line - 1] += victim
            self.pos.up(margin, self.lines)
            self.pos.x = self.pos.x_hint = new_x
            # deleting the fake end-of-file doesn't cause modification
            self.modified |= self.pos.cursor_line < len(self.lines) - 1
            _restore_lines_eof_invariant(self.lines)
        else:
            s = self.lines[self.pos.cursor_line]
            self.lines[self.pos.cursor_line] = (
                s[:self.pos.x - 1] + s[self.pos.x:]
            )
            self.pos.left(margin, self.lines)
            self.modified = True

    def delete(self, margin: Margin) -> None:
        # noop at end of the file
        if self.pos.cursor_line == len(self.lines) - 1:
            pass
        # if we're at the end of the line, collapse the line afterwards
        elif self.pos.x == len(self.lines[self.pos.cursor_line]):
            self.lines[self.pos.cursor_line] += (
                self.lines[self.pos.cursor_line + 1]
            )
            self.lines.pop(self.pos.cursor_line + 1)
            self.modified = True
        else:
            s = self.lines[self.pos.cursor_line]
            self.lines[self.pos.cursor_line] = (
                s[:self.pos.x] + s[self.pos.x + 1:]
            )
            self.modified = True

    def enter(self, margin: Margin) -> None:
        s = self.lines[self.pos.cursor_line]
        self.lines[self.pos.cursor_line] = s[:self.pos.x]
        self.lines.insert(self.pos.cursor_line + 1, s[self.pos.x:])
        self.pos.down(margin, self.lines)
        self.pos.x = self.pos.x_hint = 0
        self.modified = True

    DISPATCH = {
        curses.KEY_BACKSPACE: backspace,
        curses.KEY_DC: delete,
        ord('\r'): enter,
    }

    def c(self, wch: str, margin: Margin) -> None:
        s = self.lines[self.pos.cursor_line]
        self.lines[self.pos.cursor_line] = (
            s[:self.pos.x] + wch + s[self.pos.x:]
        )
        self.pos.right(margin, self.lines)
        self.modified = True
        _restore_lines_eof_invariant(self.lines)


def _color_test(stdscr: 'curses._CursesWindow') -> None:
    Header(File('<<color test>>'), 1, 1).draw(stdscr)

    maxy, maxx = stdscr.getmaxyx()
    if maxy < 19 or maxx < 68:  # pragma: no cover (will be deleted)
        raise SystemExit('--color-test needs a window of at least 68 x 19')

    y = 1
    for fg in range(-1, 16):
        x = 0
        for bg in range(-1, 16):
            if bg > fg:
                s = f'*{COLORS[bg, fg]:3}'
            else:
                s = f' {COLORS[fg, bg]:3}'
            stdscr.addstr(y, x, s, _color(fg, bg))
            x += 4
        y += 1
    stdscr.get_wch()


def _write_lines(
        stdscr: 'curses._CursesWindow',
        pos: Position,
        margin: Margin,
        lines: List[str],
) -> None:
    lines_to_display = min(len(lines) - pos.file_line, margin.body_lines)
    for i in range(lines_to_display):
        line_idx = pos.file_line + i
        line = lines[line_idx]
        line_x = pos.line_x()
        if line_idx == pos.cursor_line and line_x:
            line = f'«{line[line_x + 1:]}'
            if len(line) > curses.COLS:
                line = f'{line[:curses.COLS - 1]}»'
            else:
                line = line.ljust(curses.COLS)
        elif len(line) > curses.COLS:
            line = f'{line[:curses.COLS - 1]}»'
        else:
            line = line.ljust(curses.COLS)
        stdscr.insstr(i + margin.header, 0, line)
    blankline = ' ' * curses.COLS
    for i in range(lines_to_display, margin.body_lines):
        stdscr.insstr(i + margin.header, 0, blankline)


def _restore_lines_eof_invariant(lines: List[str]) -> None:
    """The file lines will always contain a blank empty string at the end to
    simplify rendering.  This should be called whenever the end of the file
    might change.
    """
    if not lines or lines[-1] != '':
        lines.append('')


def _get_lines(sio: IO[str]) -> Tuple[List[str], str, bool]:
    lines = []
    newlines = collections.Counter({'\n': 0})  # default to `\n`
    for line in sio:
        for ending in ('\r\n', '\n'):
            if line.endswith(ending):
                lines.append(line[:-1 * len(ending)])
                newlines[ending] += 1
                break
        else:
            lines.append(line)
    _restore_lines_eof_invariant(lines)
    (nl, _), = newlines.most_common(1)
    mixed = len({k for k, v in newlines.items() if v}) > 1
    return lines, nl, mixed


class Key(NamedTuple):
    wch: Union[int, str]
    key: int
    keyname: bytes


# TODO: find a place to populate these, surely there's a database somewhere
SEQUENCE_KEY = {
    '\033OH': curses.KEY_HOME,
    '\033OF': curses.KEY_END,
}
SEQUENCE_KEYNAME = {
    '\033[1;5H': b'kHOM5',  # C-Home
    '\033[1;5F': b'kEND5',  # C-End
    '\033OH': b'KEY_HOME',
    '\033OF': b'KEY_END',
    '\033[1;3A': b'kUP3',  # M-Up
    '\033[1;3B': b'kDN3',  # M-Down
    '\033[1;3C': b'kRIT3',  # M-Right
    '\033[1;3D': b'kLFT3',  # M-Left
}


def _get_char(stdscr: 'curses._CursesWindow') -> Key:
    wch = stdscr.get_wch()
    if isinstance(wch, str) and wch == '\033':
        stdscr.nodelay(True)
        try:
            while True:
                try:
                    new_wch = stdscr.get_wch()
                    if isinstance(new_wch, str):
                        wch += new_wch
                    else:  # pragma: no cover (impossible?)
                        curses.unget_wch(new_wch)
                        break
                except curses.error:
                    break
        finally:
            stdscr.nodelay(False)

        if len(wch) > 1:
            key = SEQUENCE_KEY.get(wch, -1)
            keyname = SEQUENCE_KEYNAME.get(wch, b'unknown')
            return Key(wch, key, keyname)

    key = wch if isinstance(wch, int) else ord(wch)
    keyname = curses.keyname(key)
    return Key(wch, key, keyname)


EditResult = enum.Enum('EditResult', 'EXIT NEXT PREV')


def _edit(
        stdscr: 'curses._CursesWindow',
        file: File,
        header: Header,
) -> EditResult:
    margin = Margin.from_screen(stdscr)
    status = Status()
    file.ensure_loaded(status, margin)

    while True:
        status.tick()

        if margin.header:
            header.draw(stdscr)
        _write_lines(stdscr, file.pos, margin, file.lines)
        status.draw(stdscr, margin)
        file.pos.move_cursor(stdscr, margin)

        key = _get_char(stdscr)

        if key.key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            margin = Margin.from_screen(stdscr)
            file.pos.maybe_scroll_down(margin)
        elif key.key in Position.DISPATCH:
            file.pos.DISPATCH[key.key](file.pos, margin, file.lines)
        elif key.keyname in Position.DISPATCH_KEY:
            file.pos.DISPATCH_KEY[key.keyname](file.pos, margin, file.lines)
        elif key.keyname == b'^X':
            return EditResult.EXIT
        elif key.keyname == b'kLFT3':
            return EditResult.PREV
        elif key.keyname == b'kRIT3':
            return EditResult.NEXT
        elif key.keyname == b'^Z':
            curses.endwin()
            os.kill(os.getpid(), signal.SIGSTOP)
            stdscr = _init_screen()
        elif key.key in file.DISPATCH:
            file.DISPATCH[key.key](file, margin)
        elif isinstance(key.wch, str) and key.wch.isprintable():
            file.c(key.wch, margin)
        else:
            status.update(f'unknown key: {key}', margin)


def _init_screen() -> 'curses._CursesWindow':
    # set the escape delay so curses does not pause waiting for sequences
    os.environ.setdefault('ESCDELAY', '25')
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    # <enter> is not transformed into '\n' so it can be differentiated from ^J
    curses.nonl()
    # ^S / ^Q / ^Z / ^\ are passed through
    curses.raw()
    stdscr.keypad(True)
    with contextlib.suppress(curses.error):
        curses.start_color()
    _init_colors(stdscr)
    return stdscr


def c_main(stdscr: 'curses._CursesWindow', args: argparse.Namespace) -> None:
    if args.color_test:
        return _color_test(stdscr)
    files = [File(filename) for filename in args.filenames or [None]]
    i = 0
    while files:
        i = i % len(files)
        file = files[i]
        header = Header(file, i, len(files))
        res = _edit(stdscr, file, header)
        if res == EditResult.EXIT:
            del files[i]
        elif res == EditResult.NEXT:
            i += 1
        elif res == EditResult.PREV:
            i -= 1
        else:
            raise AssertionError(f'unreachable {res}')


@contextlib.contextmanager
def make_stdscr() -> Generator['curses._CursesWindow', None, None]:
    """essentially `curses.wrapper` but split out to implement ^Z"""
    stdscr = _init_screen()
    try:
        yield stdscr
    finally:
        curses.endwin()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--color-test', action='store_true')
    parser.add_argument('filenames', metavar='filename', nargs='*')
    args = parser.parse_args()
    with make_stdscr() as stdscr:
        c_main(stdscr, args)
    return 0


if __name__ == '__main__':
    exit(main())
