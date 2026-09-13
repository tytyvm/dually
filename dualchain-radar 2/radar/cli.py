import argparse
import dataclasses
import html
import json
import logging
import sys
import time
from pathlib import Path

from .domain import Config, Snapshot
from .engine import Engine
from .feeds import read_jsonl, run_collector


def demo_events():
    start = 1800000000
    paths = {"runner": [1, 1.04, 1.09, 1.12, 1.3, 1.65, 2.45, 2.55, 2.6, 2.4],
             "failed": [1, 1.05, 1.1, 1.15, 1.0, .85, .80, .90, .70, .65]}
    for i in range(10):
        for chain in ("solana", "robinhood"):
            for name, prices in paths.items():
                yield Snapshot(chain=chain, token=f"DEMO-{name}", pool=f"DEMO-pool-{name}",
                    ts=start + 30 * i, price=.001 * prices[i], liquidity=50000,
                    market_cap=60000 * prices[i], fdv=60000 * prices[i], created_at=start - 300,
                    volume_m5=5000 + i * 500, buys_m5=30 + i * 5, sells_m5=8,
                    buyers_m5=15 + i * 3, change_m5=.2, symbol=name.upper(), source="synthetic_demo")


def write_report(report, output):
    def esc(value):
        return html.escape(str(value))
    rows = "".join(f"<tr><td>{esc(ch)}</td><td>${a['cash']:.2f}</td>"
                   f"<td>${a['estimated_equity']:.2f}</td><td>${a['realized_pnl']:.2f}</td>"
                   f"<td>{a['closed_trades']}</td><td>{a['stale_positions']}</td></tr>"
                   for ch, a in report["accounts"].items())
    events = "".join(f"<tr><td>{esc(e['ts'])}</td><td>{esc(e['kind'])}</td>"
                     f"<td>{esc(e.get('key', ''))}</td><td>{esc(e.get('reason',''))}</td></tr>"
                     for e in report["events"])
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Dualchain Radar · Paper report</title>
    <style>body{{background:#111820;color:#e9f0f6;font:16px system-ui;max-width:1050px;margin:40px auto;padding:24px}}
    h1{{font-size:38px}}small,.muted{{color:#a5b6c7}}.tag{{color:#68ddb2}}table{{width:100%;border-collapse:collapse;margin:24px 0}}
    th,td{{padding:12px;text-align:left;border-bottom:1px solid #344251}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
    .note{{padding:18px;border:1px solid #58697a;border-radius:10px}}.scroll{{overflow:auto}}</style>
    <small class="tag">RESEARCH / PAPER ONLY</small><h1>Dualchain Radar</h1>
    <p>Solana + Robinhood · {esc(report['strategy'])} · event time {report['clock']}</p>
    <div class="note">{esc(report['warning'])}</div><div class="scroll"><table>
    <tr><th>Chain</th><th>Cash</th><th>Estimated equity</th><th>Realized PnL</th><th>Closed</th><th>Stale</th></tr>{rows}</table></div>
    <h2>Open positions</h2><pre>{esc(json.dumps(report['positions'],indent=2))}</pre>
    <h2>Recent decisions and fills</h2><div class="scroll"><table><tr><th>Time</th><th>Event</th><th>Token</th><th>Reason</th></tr>{events}</table></div>
    <p class="muted">Generated snapshot, not a live dashboard. Synthetic demo returns are not performance evidence.</p></html>"""
    Path(output).write_text(page)


def main(argv=None):
    p = argparse.ArgumentParser(description="Two-chain research and paper-trading engine. No live orders.")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--db", default="data/paper.db")
    p.add_argument("--model", help="Optional research model JSON; rejects events preceding training cutoff")
    subs = p.add_subparsers(dest="command", required=True)
    subs.add_parser("init")
    d = subs.add_parser("demo")
    d.add_argument("--out", default="demo-report.html")
    r = subs.add_parser("run")
    r.add_argument("--mode", choices=["paper"], default="paper")
    r.add_argument("--once", action="store_true")
    r.add_argument("--seconds", type=int, default=0)
    replay = subs.add_parser("replay")
    replay.add_argument("file")
    replay.add_argument("--out", default="replay-report.html")
    subs.add_parser("ingest", help="Read normalized live Snapshot JSONL on stdin; paper orders only")
    subs.add_parser("status")
    subs.add_parser("pause")
    subs.add_parser("resume")
    report = subs.add_parser("report")
    report.add_argument("--out", default="paper-report.html")
    export = subs.add_parser("export")
    export.add_argument("file")
    train = subs.add_parser("train")
    train.add_argument("file")
    train.add_argument("--out", default="model.json")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = Config.load(args.config)
    if args.command == "train":
        from .learning import train
        snapshots = list(read_jsonl(args.file))
        if any(s.source == "synthetic_demo" for s in snapshots):
            p.error("Demo data cannot be used to train a trading model")
        print(json.dumps(train(snapshots, c, args.out), indent=2))
        return
    model = json.loads(Path(args.model).read_text()) if args.model else None
    e = Engine(args.db, c, model)
    try:
        if args.command in ("demo", "replay"):
            if e.state()["clock"]:
                p.error("Replay/demo requires a new empty database; choose --db with a new filename")
            events = list(demo_events()) if args.command == "demo" else list(read_jsonl(args.file))
            if not events:
                p.error("No snapshots")
            if any(b.ts < a.ts for a, b in zip(events, events[1:])):
                p.error("Replay input must be ordered by availability timestamp; sort before replay")
            for s in events:
                e.step(s)
            write_report(e.report(), args.out)
            print(json.dumps(e.report(), indent=2))
        elif args.command == "run":
            print(json.dumps(run_collector(e, c, once=args.once, duration=args.seconds), indent=2))
        elif args.command == "ingest":
            for n, line in enumerate(sys.stdin, 1):
                if line.strip():
                    s = Snapshot(**json.loads(line))
                    print(json.dumps({"line": n, "result": e.step(s, now=int(time.time()))}), flush=True)
        elif args.command == "pause":
            e.pause(True)
            print("Entries paused; pending buys canceled. Existing positions exit on later valid snapshots.")
        elif args.command == "resume":
            e.pause(False)
            print("Manual pause cleared. Drawdown and daily risk halts remain enforced.")
        elif args.command == "export":
            with open(args.file, "w") as f:
                for row in e.conn.execute("SELECT data FROM snapshots ORDER BY ts,id"):
                    f.write(row[0] + "\n")
            print(args.file)
        elif args.command == "report":
            write_report(e.report(), args.out)
            print(args.out)
        else:
            print(json.dumps(e.report(), indent=2))
    except KeyboardInterrupt:
        print("Stopped; positions and pending orders are persisted.", file=sys.stderr)
    finally:
        e.close()


if __name__ == "__main__":
    main()

