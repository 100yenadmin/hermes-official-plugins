"""Offline repro (diagnostic, no fix): does a respawned native replay an unchanged history the way the old one sent it?

Opt-in; not wired into ci.sh. The real CLI and DirectSDK in session mode against the continuity eval's loopback
peer (no account, no inference, synthetic public content only). Each shape builds the same short conversation, leaves
one tool call pending on a live native, then makes the step that retires it:
  C0  control: the pool shuts down (logged kill), the next step respawns cold
  S1  the pending call returns an image (multimodal tool result): unexpected suffix, a new native replays
  S2  a user message arrives with the pending call's result (/steer, an inbound message): unexpected suffix, replay
  S3  the pool loses the native with no kill line (as in a new gateway process): cold respawn, replay
  S3b the native process dies under the pool (killed when the next checkout finds it dead): cold respawn, replay
For each it compares the old native's last forwarded request with the new native's first, and checks the cache
entries every earlier request wrote (one per cache mark) against the new request's marks, walking back at most
LOOKBACK blocks from each mark. Blocks are flattened in cached-prefix order: each tool, each system block, then each
message's content blocks; cache_control is not prompt text and is ignored when comparing prefixes.
Prints only positions, roles, block types, lengths and short hashes.
"""
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import directsdk_cache_continuity as cc  # noqa: E402
from directsdk_cache_wire import LOOKBACK, content  # noqa: E402

PNG = "data:image/png;base64,iVBORw0KGgo="  # as tests/test_directsdk_vision.py sends it
SHAPES = {"C0": "control: logged shutdown, cold respawn", "S1": "image tool result", "S2": "user message with the pending result",
          "S3": "pool loss, no kill line", "S3b": "native process died"}
RESULT = ("Public synthetic result\n" * 100)[:2048]


def flat(wire):
    """(where, block) in cached-prefix order: tools, system blocks, then message content blocks."""
    out = [(("tools", i, None, None), tool) for i, tool in enumerate(wire.get("tools") or [])]
    system = wire.get("system") or []
    out += [(("system", j, None, None), block) for j, block in enumerate(cc.blocks(system))]
    for i, message in enumerate(wire["messages"]):
        out += [(("messages", i, j, message.get("role")), block) for j, block in enumerate(cc.blocks(message["content"]))]
    return out


def key(entry):
    (section, i, _, role), block = entry
    return json.dumps([section, i, role, content(block)], sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def prefixes(keys):
    """Running digest of the flattened prefix ending at each position."""
    running, out = hashlib.sha256(), []
    for k in keys:
        running.update(hashlib.sha256(k.encode()).digest())
        out.append(running.copy().hexdigest()[:16])
    return out


def marks(entries):
    return [p for p, (_, block) in enumerate(entries) if isinstance(block, dict) and "cache_control" in block]


def describe(entries, p):
    if p is None or p >= len(entries):
        return None
    (section, i, j, role), block = entries[p]
    text = "".join(n.get("text", n.get("thinking", "")) for n in cc.nodes(block) if isinstance(n, dict))
    return {"flat": p, "section": section, "message_index": i, "block_index": j, "role": role,
            "type": block.get("type", "tool") if isinstance(block, dict) else type(block).__name__,
            "inner_types": sorted({n["type"] for n in cc.nodes(block) if isinstance(n, dict) and isinstance(n.get("type"), str)}),
            "json_chars": len(json.dumps(content(block), ensure_ascii=False)), "text_chars": len(text),
            "sha256": hashlib.sha256(key(entries[p]).encode()).hexdigest()[:12]}


def analyse(wires, old_indices, new_index):
    old, new = wires[old_indices[-1]], wires[new_index]
    fo, fn = flat(old), flat(new)
    ko, kn = [key(e) for e in fo], [key(e) for e in fn]
    po, pn = prefixes(ko), prefixes(kn)
    fixed = len(new.get("tools") or []) + len(cc.blocks(new.get("system") or []))
    first_diff = next((p for p in range(len(ko)) if p >= len(kn) or ko[p] != kn[p]), None)
    n_old = len(old["messages"])
    raw_equal = old["messages"] == new["messages"][:n_old]
    content_equal = content(old["messages"]) == content(new["messages"][:n_old])
    # Cache entries: one per mark of every request before the new native's first (any native of the conversation).
    entries = {}
    for index in range(new_index):
        f = flat(wires[index])
        pref = prefixes([key(e) for e in f])
        for q in marks(f):
            entries.setdefault((q, pref[q]), index)
    new_marks = marks(fn)
    hits = sorted({q for p in new_marks for q in range(max(0, p - LOOKBACK), p + 1) if (q, pn[q]) in entries})
    # Every entry whose bytes the new request still matches, whether or not a mark reaches it.
    matching = sorted(q for (q, digest), _ in entries.items() if q < len(pn) and pn[q] == digest)
    tails = []
    for index in old_indices:
        f = flat(wires[index])
        t = len(f) - 1
        same = t < len(pn) and prefixes([key(e) for e in f])[t] == pn[t]
        reach = [p for p in new_marks if p - LOOKBACK <= t <= p]
        tails.append({"upstream_index": index + 1, "tail": t, "prefix_equal_to_tail": same, "new_marks_within_reach": reach})
    deepest, deepest_match = (hits[-1] if hits else None), (matching[-1] if matching else None)
    last_old_mark = (marks(fo) or [None])[-1]
    reach = f"deepest reachable entry {deepest} of {len(fn)} blocks (fixed tools+system = {fixed}, old native's last mark {last_old_mark})"
    if first_diff is not None:
        d = describe(fn, first_diff) or {}
        o = describe(fo, first_diff) or {}
        reading = (f"bytes differ at block {first_diff}: message {o.get('message_index')} role {o.get('role')}/{d.get('role')} "
                   f"type {o.get('type')}/{d.get('type')} json_chars {o.get('json_chars')}/{d.get('json_chars')}; {reach}")
    elif deepest is not None and last_old_mark is not None and deepest >= last_old_mark:
        reading = f"bytes equal, replay would hit: {reach}"
    else:
        reading = f"bytes equal, no mark within reach: {reach}, deepest matching entry {deepest_match}"
    return {"reading": reading, "fixed_blocks": fixed, "old_last_upstream_index": old_indices[-1] + 1,
            "new_first_upstream_index": new_index + 1, "old_blocks": len(fo), "new_blocks": len(fn),
            "old_messages": n_old, "new_messages": len(new["messages"]),
            "message_prefix_byte_identical": raw_equal, "message_prefix_identical_ignoring_cache_control": content_equal,
            "first_differing_block": {"old": describe(fo, first_diff), "new": describe(fn, first_diff)} if first_diff is not None else None,
            "old_marks": [describe(fo, p) | {"flat": p} for p in marks(fo)], "new_marks": [describe(fn, p) | {"flat": p} for p in new_marks],
            "entries_written_before": len(entries), "reachable_hits": hits, "matching_entries": matching[-5:],
            "deepest_reachable_hit": deepest, "deepest_matching_entry": deepest_match, "old_native_tails": tails,
            "system_blocks": [describe(fn, p) for p in range(len(new.get("tools") or []), fixed)],
            "first_message_blocks": [describe(fn, p) for p, e in enumerate(fn) if e[0][0] == "messages" and e[0][1] == 0],
            "new_tail_blocks": [describe(fn, p) for p in range(max(fixed, len(fn) - 4), len(fn))]}


def negative_control(wires, old_indices, new_index):
    """The same analysis after changing one early history block of the new request: it must read "bytes differ"."""
    wires = json.loads(json.dumps(wires))
    message = wires[new_index]["messages"][2]
    message["content"] = cc.blocks(message["content"])
    first = message["content"][0]
    first["text" if "text" in first else "content"] = "PUBLIC CHANGED " + str(first.get("text", first.get("content")))
    return analyse(wires, old_indices, new_index)["reading"]


def shape(native, binary, model, name, log, subscription, turns=1):
    report = {"name": SHAPES[name], "steps": []}
    native._session_shutdown()
    native._session_failures, native._session_off = 0, False
    with tempfile.TemporaryDirectory(prefix=f"replay-repro-{name}-") as tmp, ThreadingHTTPServer(("127.0.0.1", 0), cc.Peer) as peer:
        peer.wires, peer.tool_count, peer.paths, peer.signers = [], 0, [], {}
        peer.timings, peer.stream, peer.stream_gap, peer.long_tool = [], False, 0, False
        worker = threading.Thread(target=peer.serve_forever, daemon=True)
        worker.start()
        client = native.Client(command=str(binary), env=cc.isolated_env(tmp, peer.server_port, True, subscription), timeout=45)

        def request(messages, tool_count, label):
            peer.tool_count = tool_count
            start, logs = len(peer.wires), len(log.events)
            result = client.create(model=model, messages=messages, tools=cc.TOOLS)
            events = log.events[logs:]
            steps = [re.search(r"session=(\w+):(\d+) mode=(\S+)", e) for e in events if e.startswith("session step:")]
            report["steps"].append({"label": label, "mode": steps[-1].group(3) if steps else "no-step-line",
                                    "pid": steps[-1].group(2) if steps else None, "wires": list(range(start, len(peer.wires))),
                                    "lines": [re.sub(r" (tools|sys|launch|body|id)=\S+", "", e) for e in events if not e.startswith("session step:")]})
            return result.choices[0].message.model_dump()

        def answer(assistant, history, value=RESULT):
            history.append(assistant)
            history.extend({"role": "tool", "tool_call_id": c["id"], "content": value} for c in assistant.get("tool_calls") or [])

        try:
            history = [{"role": "system", "content": cc.SYSTEM}]
            for turn in range(1, turns + 1):
                history.append({"role": "user", "content": f"Public main question {turn}: run the probes."})
                for round_number, tool_count in enumerate((2, 1, 0), 1):
                    answer(request(history, tool_count, f"t{turn}r{round_number}"), history)
            history.append({"role": "user", "content": f"Public main question {turns + 1}: run the probes."})
            pending = request(history, 1, "t2r1 (call left pending)")
            old_pid = report["steps"][-1]["pid"]
            history.append(pending)
            (call,) = pending["tool_calls"]
            result = {"role": "tool", "tool_call_id": call["id"], "content": RESULT}
            if name == "S1":
                result["content"] = [{"type": "text", "text": "Public synthetic screenshot:"}, {"type": "image_url", "image_url": {"url": PNG}}]
            history.append(result)
            if name == "S2":
                history.append({"role": "user", "content": "Public steer: also check the second probe."})
            if name == "C0":
                native._session_shutdown()
            elif name in ("S3", "S3b"):
                with native._session_lock:
                    (session,) = native._session_pool
                    if name == "S3":
                        native._session_pool.remove(session)
                        session.killed = True  # gone with its process, as in a new gateway process: no kill line
                native.kill_process_tree(session.process)
                session.process.wait(timeout=10)
            request(history, 0, "shape step")
            shaped = report["steps"][-1]
            old = [w for s in report["steps"][:-1] if s["pid"] == old_pid for w in s["wires"]]
            report["old_pid_differs_from_new"] = shaped["pid"] != old_pid
            report["shape_mode"] = shaped["mode"]
            if old and shaped["wires"]:
                report["analysis"] = analyse(peer.wires, old, shaped["wires"][0])
                report["negative_control_reading"] = negative_control(peer.wires, old, shaped["wires"][0])
            else:
                report["error"] = "no old or new request captured"
        except Exception as error:
            report["error"] = {"type": type(error).__name__, "message": str(error)[:300]}
        finally:
            client.close()
            native._session_shutdown()
            peer.shutdown()
            worker.join(timeout=5)
            report["requests"] = len(peer.wires)
            report["peer_paths"] = sorted({p["path"] for p in peer.paths})
    return report


def run_isolated(args):
    root, binary = args.root.resolve(), args.binary.resolve()
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("replay_repro_directsdk", root / "directsdk.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    log = cc.SessionLog()
    logger = logging.getLogger(native.__name__)
    logger.addHandler(log)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with tempfile.TemporaryDirectory(prefix="replay-repro-version-") as tmp:
        version = subprocess.check_output([str(binary), "--version"], env=cc.isolated_env(tmp, 1, False),
                                          stdin=subprocess.DEVNULL, text=True, timeout=15).strip()
    output = {"native_version": version, "subscription_emulated": args.subscription, "model": args.model, "warm_turns": args.turns,
              "lookback_blocks": LOOKBACK, "synthetic_offline_not_cache_measurement": True, "shapes": {}}
    for name in args.only:
        output["shapes"][name] = shape(native, binary, args.model, name, log, args.subscription, args.turns)
        print(f"{name}: {output['shapes'][name].get('shape_mode')} | {output['shapes'][name].get('analysis', {}).get('reading')}"
              f" | negative control: {output['shapes'][name].get('negative_control_reading')}",
              file=sys.stderr, flush=True)
    logger.removeHandler(log)
    return output


def run(args):
    with tempfile.TemporaryDirectory(prefix="replay-repro-runner-") as tmp:
        saved = dict(os.environ)
        env = cc.isolated_env(tmp, 1, False)
        env.update(TMPDIR=tempfile.gettempdir(), HERMES_DISABLE_LAZY_INSTALLS="1")
        os.environ.clear()
        os.environ.update(env)
        try:
            return run_isolated(args)
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--subscription", action="store_true", help="emulate a subscription login offline (public fixtures)")
    parser.add_argument("--only", default=",".join(SHAPES))
    parser.add_argument("--turns", type=int, default=1, help="complete turns (3 rounds each) before the pending call")
    args = parser.parse_args()
    args.only = args.only.split(",")
    if any(name not in SHAPES for name in args.only):
        parser.error("--only takes " + ",".join(SHAPES))
    print(json.dumps(run(args), indent=1))
