"""Fake programs a test starts as a real process (VM pass, 2026-10-23).

POSIX runs a script by its shebang; Windows runs only .exe/.com and, through cmd.exe, .cmd/.bat.
So on Windows a fake is a `.cmd` next to its Python source (`<name>.py`) that runs it with this
Python and passes the arguments, standard input and exit code through. Each helper returns the
path the test puts in its config (on Windows the .cmd)."""
import os
import shutil
import sys
from pathlib import Path

WINDOWS = os.name == "nt"


def python(path, source):
    """A program running Python `source`; returns the path to start."""
    path = Path(path)
    if not WINDOWS:
        path.write_text("#!%s\n%s" % (sys.executable, source))
        path.chmod(0o700)
        return path
    script = path.with_name(path.name + ".py")
    script.write_text(source, encoding="utf-8")
    command = path.with_name(path.name + ".cmd")
    command.write_text('@"%s" "%s" %%*\r\n@exit /b %%errorlevel%%\r\n' % (sys.executable, script))
    return command


def copy(path, source_file):
    """A program running the Python file `source_file` (copied); returns the path to start."""
    if not WINDOWS:
        shutil.copyfile(source_file, path)
        Path(path).chmod(0o700)
        return Path(path)
    return python(path, Path(source_file).read_text(encoding="utf-8"))


def script(command):
    """The Python file a program made here runs (POSIX: the program itself)."""
    command = Path(command)
    return command.with_suffix(".py") if WINDOWS and command.suffix == ".cmd" else command


def name(command):
    """The name the test gave the program (without the Windows .cmd)."""
    command = Path(command)
    return command.stem if WINDOWS and command.suffix == ".cmd" else command.name


def stub(path):
    """An installed-looking program that does nothing; returns its path."""
    path = Path(path)
    if not WINDOWS:
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path
    command = path.with_name(path.name + ".cmd")
    command.write_text("@exit /b 0\r\n")
    return command
