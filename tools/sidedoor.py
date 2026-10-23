"""The frozen build's entry point (E9.T1): the `sidedoor` console command, orchestrator.cli.

Nuitka compiles its main script as `__main__`. orchestrator.py is imported by name by many modules
(`from orchestrator import profile_key`), so compiling it as the main script would load it twice
(`__main__` and `orchestrator`, each with its own state). This launcher imports it once, by name.
Not a product module: tools/build_macos.py builds from it; from source run `sidedoor` or
`python3.13 orchestrator.py`.
"""

import sys

# No .pyc beside the uncompiled libraries (pystray, fpdf; E9.T1 AP2): the bundle stays as built (and
# signed), and a user's replacement of those folders is read as they left it.
sys.dont_write_bytecode = True

import orchestrator  # noqa: E402

orchestrator.cli()
