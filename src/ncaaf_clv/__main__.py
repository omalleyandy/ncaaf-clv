"""``python -m ncaaf_clv`` -- the ``ncaaf-clv`` console script without the script shim.

Equivalent to ``ncaaf-clv ...``. Use it where the shim cannot execute: Windows Smart App Control on a client
machine refuses uv's unsigned console-script launchers ("An Application Control policy has blocked this file",
os error 4551) while ``python.exe`` itself is fine.
"""

from __future__ import annotations

from ncaaf_clv.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
