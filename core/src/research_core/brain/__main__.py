"""python -m research_core.brain install | seed <coworld-id> | tidy"""

from __future__ import annotations

import argparse
import datetime as dt
import os

from research_core import layout
from research_core.brain import install, seeds, tidy

ap = argparse.ArgumentParser(prog="python -m research_core.brain")
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("install")
sd = sub.add_parser("seed")
sd.add_argument("coworld")
sd.add_argument("--skills")
sd.add_argument("--howto")
td = sub.add_parser("tidy")
td.add_argument("--today")
a = ap.parse_args()
ws = layout.workspace_root(os.environ)
if a.cmd == "install":
    print(f"brain: {len(install.install(ws))} file(s) created")
elif a.cmd == "seed":
    for key, status in seeds.install_all(ws, a.coworld, a.skills, a.howto).items():
        print(f"{key}: {status}")
else:
    print(tidy.tidy(ws, dt.date.fromisoformat(a.today) if a.today else None))
