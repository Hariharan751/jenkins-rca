"""CLI to query the per-RCA outcome log.

Usage on jenkins-rca host:
    python3 -m jenkins_rca.cli_outcomes recent          # last 20 RCAs
    python3 -m jenkins_rca.cli_outcomes signals 7       # signal counts last 7 days
    python3 -m jenkins_rca.cli_outcomes by-class 14     # rows per error_class, 14d
    python3 -m jenkins_rca.cli_outcomes cost 30         # cost rollup last 30 days
    python3 -m jenkins_rca.cli_outcomes show <id>       # full row + trace_path

Use the trace_path column to open the per-build trace file for any RCA.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import outcome_log


def _conn():
    return sqlite3.connect(str(outcome_log.DB_PATH))


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_recent(n: int = 20):
    conn = _conn()
    rows = conn.execute(
        "SELECT id, ts, job, build, error_class, iters, tool_calls, "
        "cost_usd, quality FROM outcomes ORDER BY ts DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        print("(no outcomes logged yet)")
        return
    print(f"{'id':>5}  {'when':<16}  {'job':<40}  {'build':>6}  "
          f"{'class':<14}  {'iters':>5}  {'tools':>5}  {'$':>7}  quality")
    print("-" * 120)
    for r in rows:
        rid, ts, job, build, cls, iters, tcalls, cost, quality = r
        print(f"{rid:>5}  {_fmt_ts(ts):<16}  {(job or '')[:40]:<40}  "
              f"{build:>6}  {(cls or '?')[:14]:<14}  {iters or 0:>5}  "
              f"{tcalls or 0:>5}  ${cost or 0:>6.4f}  {quality or '-'}")


def cmd_signals(days: int = 7):
    conn = _conn()
    rows = conn.execute(
        """
        SELECT je.value AS signal, COUNT(*) AS n
          FROM outcomes, json_each(outcomes.failure_signals) AS je
         WHERE ts > strftime('%s','now', ?)
         GROUP BY je.value
         ORDER BY n DESC
        """,
        (f'-{int(days)} days',),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE ts > strftime('%s','now', ?)",
        (f'-{int(days)} days',),
    ).fetchone()[0]
    print(f"Failure-signal frequency (last {days} days, {total} total RCAs):")
    if not rows:
        print("  (none)")
        return
    for sig, n in rows:
        pct = (n / total * 100) if total else 0
        print(f"  {sig:<32}  {n:>4}   ({pct:.0f}% of runs)")


def cmd_by_class(days: int = 14):
    conn = _conn()
    rows = conn.execute(
        """
        SELECT error_class, COUNT(*), AVG(iters), AVG(tool_calls),
               AVG(cost_usd), SUM(CASE quality='correct' WHEN 1 THEN 1 ELSE 0 END)
          FROM outcomes
         WHERE ts > strftime('%s','now', ?)
         GROUP BY error_class
         ORDER BY 2 DESC
        """,
        (f'-{int(days)} days',),
    ).fetchall()
    print(f"By error_class (last {days} days):")
    print(f"  {'class':<16}  {'n':>4}  {'avg_iters':>10}  {'avg_tools':>10}  "
          f"{'avg_cost':>9}  {'correct':>7}")
    for cls, n, ai, at, ac, cor in rows:
        print(f"  {(cls or '?')[:16]:<16}  {n:>4}  {(ai or 0):>10.1f}  "
              f"{(at or 0):>10.1f}  ${ (ac or 0):>7.4f}  {cor or 0:>7}")


def cmd_cost(days: int = 30):
    conn = _conn()
    row = conn.execute(
        """
        SELECT COUNT(*), SUM(cost_usd), AVG(cost_usd), MAX(cost_usd),
               SUM(tokens_in), SUM(tokens_out)
          FROM outcomes
         WHERE ts > strftime('%s','now', ?)
        """,
        (f'-{int(days)} days',),
    ).fetchone()
    n, total, avg, mx, tin, tout = row
    if not n:
        print(f"(no outcomes in last {days} days)")
        return
    print(f"Cost rollup last {days} days:")
    print(f"  RCAs:         {n}")
    print(f"  total cost:   ${total or 0:.4f}")
    print(f"  avg cost:     ${avg or 0:.4f}")
    print(f"  max cost:     ${mx or 0:.4f}")
    print(f"  tokens in:    {tin or 0:,}")
    print(f"  tokens out:   {tout or 0:,}")


def cmd_show(rid: int):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM outcomes WHERE id = ?", (rid,),
    ).fetchone()
    if not row:
        print(f"no outcome with id={rid}")
        return
    cols = [d[0] for d in conn.execute("SELECT * FROM outcomes WHERE id=?", (rid,)).description]
    for c, v in zip(cols, row):
        if c == "ts":
            v = f"{v} ({_fmt_ts(v)})"
        print(f"  {c:<18}: {v}")


def main():
    args = sys.argv[1:]
    if not args:
        cmd_recent(20)
        return
    cmd, *rest = args
    if cmd == "recent":
        cmd_recent(int(rest[0]) if rest else 20)
    elif cmd == "signals":
        cmd_signals(int(rest[0]) if rest else 7)
    elif cmd == "by-class":
        cmd_by_class(int(rest[0]) if rest else 14)
    elif cmd == "cost":
        cmd_cost(int(rest[0]) if rest else 30)
    elif cmd == "show":
        if not rest:
            print("usage: show <id>")
            return
        cmd_show(int(rest[0]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
