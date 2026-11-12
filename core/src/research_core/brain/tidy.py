"""Tidy the cogent brain. Plain files, no model calls, never deletes (moves to brain/archive/).

    python -m research_core.brain tidy [--today YYYY-MM-DD]  (workspace = IDE_WORKSPACE)

Port of cogent-howto brain-tidy.sh:
  - failed.md, wins.md, gotchas.md over 8 KB: oldest entries move, one-line summary left.
  - rules.md over 40 rules, ideas.md over 30, hypotheses.md over 30: oldest lines move.
  - journal lines `[provisional]` with (seen: 1) older than 30 days move.
  - claims: unconfirmed for 90 days drop a level (HIGH>MED>LOW); LOW older than 90 days move;
    over 60 claims, LOW then oldest move.
  - handoff/ files older than 30 days and log.md lines beyond 100 move.
  - research/notes with `status: unverified` older than 60 days move.
Lists only (never promotes): lessons seen 2+ times, rules older than 90 days.
Writes brain/../tidy-report.md. A STOP file in the workspace stops it.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

CAP_BYTES, CAP_RULES, CAP_CLAIMS, CAP_LIST, CAP_LOG = 8192, 40, 60, 30, 100
SUMMARY = "- [archived] "
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
LEVELS = ["LOW", "MED", "HIGH"]


def _d(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


class Tidy:
    def __init__(self, ws, today):
        self.ws, self.brain, self.today = Path(ws), Path(ws) / "brain", today
        self.arch = self.brain / "archive"
        self.report: list[str] = []
        self.ledger: list[str] = []

    def archive(self, name, text):
        self.arch.mkdir(parents=True, exist_ok=True)
        with open(self.arch / name, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def note(self, file, what):
        self.ledger.append(f"{self.today} | {file} | {what} | tidy")
        self.report.append(f"- {file}: {what}")

    def entries_file(self, name):
        """## entry files, newest first."""
        p = self.brain / name
        if not p.exists() or len(p.read_bytes()) <= CAP_BYTES:
            return
        head, entries, summ, cur = [], [], [], None
        for ln in p.read_text(encoding="utf-8").split("\n"):
            if ln.startswith("## "):
                cur = [ln]
                entries.append(cur)
            elif ln.startswith(SUMMARY):
                summ.append(ln)
            elif cur is None:
                head.append(ln)
            else:
                cur.append(ln)

        def build():
            body = "\n".join(head).rstrip("\n") + "\n\n"
            body += "".join("\n".join(e).rstrip("\n") + "\n" for e in entries)
            return body + ("\n" + "\n".join(summ) + "\n" if summ else "")

        moved = 0
        while len(build().encode()) > CAP_BYTES and len(entries) > 1:
            e = entries.pop()
            self.archive(name, "\n".join(e))
            summ.append(f"{SUMMARY}{e[0][3:].strip()[:80]} (see archive/{name})")
            moved += 1
        if moved:
            p.write_text(build(), encoding="utf-8")
            self.note(name, f"{moved} oldest entries archived (over 8 KB)")

    def lines_file(self, name, cap, bullet=r"^- ", datekey=None):
        """One-line bullets, newest first; move the oldest beyond cap."""
        p = self.brain / name
        if not p.exists():
            return
        lines = p.read_text(encoding="utf-8").split("\n")
        inside, idx = False, []
        for i, ln in enumerate(lines):  # skip the format comment, its example lines are not data
            if ln.lstrip().startswith("<!--"):
                inside = True
            if not inside and re.match(bullet, ln) and not ln.startswith(SUMMARY):
                idx.append(i)
            if "-->" in ln:
                inside = False
        if len(idx) <= cap:
            return
        drop = idx[cap:]
        self.archive(name, "\n".join(lines[i] for i in drop))
        keep = [ln for i, ln in enumerate(lines) if i not in set(drop)]
        p.write_text("\n".join(keep), encoding="utf-8")
        self.note(name, f"{len(drop)} oldest lines archived (over {cap})")

    def log_file(self):
        p = self.brain / "log.md"
        if not p.exists():
            return
        lines = p.read_text(encoding="utf-8").split("\n")
        idx = [i for i, ln in enumerate(lines) if DATE.match(ln)]
        if len(idx) > CAP_LOG:
            drop = set(idx[CAP_LOG:])
            self.archive("log.md", "\n".join(lines[i] for i in sorted(drop)))
            p.write_text(
                "\n".join(ln for i, ln in enumerate(lines) if i not in drop), encoding="utf-8"
            )
            self.note("log.md", f"{len(drop)} oldest lines archived")

    def journal(self):
        seen2 = []
        for p in sorted((self.brain / "journal").glob("*.md")):
            keep, move = [], []
            for ln in p.read_text(encoding="utf-8").split("\n"):
                m = DATE.match(ln.lstrip("- "))
                s = re.search(r"\(seen: (\d+)\)", ln)
                if m and s and "[provisional]" in ln:
                    if int(s.group(1)) >= 2:
                        seen2.append(ln.strip())
                    elif (self.today - (_d(m.group(1)) or self.today)).days > 30:
                        move.append(ln)
                        continue
                keep.append(ln)
            if move:
                self.archive(f"journal-{p.name}", "\n".join(move))
                p.write_text("\n".join(keep), encoding="utf-8")
                self.note(
                    f"journal/{p.name}",
                    f"{len(move)} unconfirmed provisional lessons archived (30 days)",
                )
        if seen2:
            self.report.append(
                "- Candidates to promote to claims (seen 2+ times; need an outcome or the user):"
            )
            self.report += [f"  {x}" for x in seen2]

    def claims(self):
        p = self.brain / "claims.md"
        if not p.exists():
            return
        out, move, decayed, inside = [], [], 0, False
        for ln in p.read_text(encoding="utf-8").split("\n"):
            if ln.lstrip().startswith("<!--"):
                inside = True
            was_inside = inside
            if "-->" in ln:
                inside = False
            if was_inside or not (ln.startswith("- ") and "| confidence:" in ln):
                out.append(ln)
                continue
            conf = re.search(r"confidence: (HIGH|MED|LOW)", ln)
            since = _d((re.search(r"since: (\S+)", ln) or [None, None])[1])
            conf_at = _d((re.search(r"confirmed: (\S+)", ln) or [None, None])[1])
            dec_at = _d((re.search(r"decayed: (\S+)", ln) or [None, None])[1])
            clock = max([d for d in (since, conf_at, dec_at) if d], default=self.today)
            level = conf.group(1) if conf else "LOW"
            if (self.today - clock).days > 90:
                if level == "LOW":
                    move.append(ln)
                    continue
                new = LEVELS[LEVELS.index(level) - 1]
                ln = re.sub(r"confidence: (HIGH|MED)", f"confidence: {new}", ln, count=1)
                ln = re.sub(r"\s*\| decayed: \S+", "", ln) + f" | decayed: {self.today}"
                decayed += 1
            out.append(ln)
        idx = [
            i
            for i, ln in enumerate(out)
            if ln.startswith("- ") and "| confidence:" in ln and "<claim>" not in ln
        ]
        if len(idx) > CAP_CLAIMS:
            order = sorted(idx, key=lambda i: ("LOW" not in out[i].split("confidence:")[1][:5], -i))
            drop = set(order[: len(idx) - CAP_CLAIMS])
            move += [out[i] for i in sorted(drop)]
            out = [ln for i, ln in enumerate(out) if i not in drop]
        if move:
            self.archive("claims.md", "\n".join(move))
        if move or decayed:
            p.write_text("\n".join(out), encoding="utf-8")
            self.note("claims.md", f"{decayed} decayed, {len(move)} archived")

    def old_rules(self):
        p = self.brain / "rules.md"
        if p.exists():
            old = [
                ln
                for ln in p.read_text(encoding="utf-8").split("\n")
                if ln.startswith("- ")
                and (m := re.search(r"added (\d{4}-\d{2}-\d{2})", ln))
                and (self.today - _d(m.group(1))).days > 90
            ]
            if old:
                self.report.append("- Rules older than 90 days (keep or drop? not touched):")
                self.report += [f"  {x}" for x in old]

    def handoffs_and_notes(self):
        for sub, pat, days, only in (
            ("brain/handoff", "*.md", 30, None),
            ("research/notes", "*.md", 60, "status: unverified"),
        ):
            for f in sorted((self.ws / sub).glob(pat)):
                if f.name == "README.md":
                    continue
                m = DATE.search(f.name)
                text = f.read_text(encoding="utf-8")
                when = (
                    _d(m.group(1))
                    if m
                    else _d((re.search(r"fetched: (\S+)", text) or [None, None])[1])
                )
                if when and (self.today - when).days > days and (only is None or only in text):
                    self.archive(f"{sub.replace('/', '-')}-{f.name}", text)
                    f.unlink()
                    self.note(sub + "/" + f.name, f"archived (older than {days} days)")

    def run(self):
        for n in ("failed.md", "wins.md", "gotchas.md"):
            self.entries_file(n)
        self.lines_file("rules.md", CAP_RULES)
        self.lines_file("ideas.md", CAP_LIST)
        self.lines_file("hypotheses.md", CAP_LIST)
        self.log_file()
        self.journal()
        self.claims()
        self.old_rules()
        self.handoffs_and_notes()
        if self.ledger:
            p = self.brain / "log.md"
            text = p.read_text(encoding="utf-8") if p.exists() else "# Write ledger\n"
            first = next((i for i, ln in enumerate(text.split("\n")) if DATE.match(ln)), None)
            lines = text.split("\n")
            lines[first if first is not None else len(lines) : 0] = self.ledger
            p.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        body = (
            f"# Tidy report, {self.today}\n\n"
            + ("\n".join(self.report) if self.report else "Nothing to tidy.")
            + "\n"
        )
        (self.ws / "tidy-report.md").write_text(body, encoding="utf-8")
        return body


def tidy(workspace, today=None) -> str:
    ws = Path(workspace)
    if (ws / "STOP").exists():
        return "STOP file present, not running\n"
    if not (ws / "brain").is_dir():
        raise SystemExit(f"brain_tidy: {ws}/brain not found")
    return Tidy(ws, today or dt.date.today()).run()
