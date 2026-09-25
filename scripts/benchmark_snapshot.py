#!/usr/bin/env python3
"""Compare complete snapshot validation on temporary, synthetic Git channels."""
import argparse
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time


def load(package, name):
    spec = importlib.util.spec_from_file_location(name, package / "ai_comm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def append_fixture(ac, channel, start, stop, body):
    """Fast-import produces one commit per message, bypassing timed send work."""
    parent = channel.snapshot().head
    stream = bytearray()
    stamp = ac.now()
    for seq in range(start, stop + 1):
        draft = {"act": "ASK", "to": ["beta"], "topic": "benchmark",
                 "summary": "Review a synthetic measurement with its uncertainty.", "body": body}
        message = dict(draft, v=2, id=f"{seq:08d}-alpha", seq=seq, ts=stamp,
                       session="alpha-benchmark", generation=1, key=f"fixture-{seq}",
                       draft_sha256=ac.sha(ac.canonical(draft)))
        message["from"] = "alpha"
        data = ac.canonical(message)
        title = f"Synthetic message {seq}\n".encode()
        stream.extend(b"commit refs/heads/main\ncommitter benchmark <benchmark@local.invalid> 1700000000 +0000\n")
        stream.extend(f"data {len(title)}\n".encode() + title)
        if seq == start:
            stream.extend(f"from {parent}\n".encode())
        stream.extend(f"M 100644 inline messages/{message['id']}.json\ndata {len(data)}\n".encode())
        stream.extend(data + b"\n")
    ac.git(channel.path, "fast-import", "--quiet", data=bytes(stream))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, help="trusted local package checkout")
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--messages", type=int, nargs="+", default=[100, 1000, 3000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20 or not all(1 <= n <= 20000 for n in args.messages):
        parser.error("repeats must be 1..20; message counts must be 1..20000")
    baseline, candidate = load(args.baseline.resolve(), "baseline"), load(args.candidate.resolve(), "candidate")
    report = {"python": platform.python_version(),
              "git": subprocess.check_output(["git", "--version"], text=True).strip(),
              "measurement": "complete snapshot validation; warm local filesystem; median seconds",
              "fixture": "one commit/message, short ASK messages, no sources or agreement events",
              "results": []}
    bodies = {"ascii": "A short observation for a representative review message. " * 5,
              "multilingual": "Une mesure approximative, français, română, 日本語. " * 5}
    for kind, body in bodies.items():
        with tempfile.TemporaryDirectory(prefix="ai-comm-benchmark-") as directory:
            path = Path(directory) / "channel"
            candidate.Channel.init(path, "Synthetic scale benchmark", ["alpha", "beta"])
            current = candidate.Channel(path)
            for actor in ("alpha", "beta"):
                current.join(actor, "test", actor + "-benchmark")
            channels = {"baseline": baseline.Channel(path), "candidate": current}
            previous = 0
            for count in sorted(set(args.messages)):
                append_fixture(candidate, current, previous + 1, count, body)
                samples = {name: [] for name in channels}
                for channel in channels.values():
                    assert len(channel.snapshot().messages) == count
                for repetition in range(args.repeats):
                    order = list(channels) if repetition % 2 == 0 else list(reversed(channels))
                    for name in order:
                        start = time.perf_counter()
                        state = channels[name].snapshot()
                        samples[name].append(time.perf_counter() - start)
                        assert len(state.messages) == count
                result = {"text": kind, "messages": count, "body_chars": len(body),
                          "samples_seconds": samples,
                          "median_seconds": {name: statistics.median(values) for name, values in samples.items()}}
                report["results"].append(result)
                previous = count
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
