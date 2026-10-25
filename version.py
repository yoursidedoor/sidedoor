"""Sidedoor's installed version, one source for every caller (E9.T3 update check, `sidedoor doctor`).

A plain module constant, so it is the same in a source checkout, a pip install and a Nuitka build
(which bundles neither pyproject.toml nor dist-info). tests/test_version.py fails when it differs
from pyproject.toml's [project] version. Releases use plain N.N.N only (no pre-release or build
suffix): the update check compares exactly three numbers."""

__version__ = "0.2.0"


def current():
    """The installed version, e.g. "1.2.0"."""
    return __version__
