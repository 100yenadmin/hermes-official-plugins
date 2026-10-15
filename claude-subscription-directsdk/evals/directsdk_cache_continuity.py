"""Offline multi-turn native cache-continuity rehearsal B0 (no account credentials).

Run with --root pointing at exactly one DirectSDK checkout per Python process.
Uses the real CLI and DirectSDK against a synthetic loopback Messages peer.
Proves exact marker-prefix continuity, signed-thinking replay and caller isolation
for six turns in eight scenarios, not vendor cache hits, costs or real-model
behaviour. Usage and signatures are public synthetic fixtures. Only compact
hash receipts are printed; no raw prompts, traces or credentials are retained.
With --subscription, emulate native's account context using public fixtures;
this does not authenticate a real account.
"""
import argparse
from collections import Counter
import copy
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

from directsdk_cache_wire import Peer as WirePeer, USAGE, content, cut, digest, nodes


LONG_NAME = "public_probe_" + "x" * 48  # 61 chars: native must use #57's deterministic alias.
LONG_ALIAS = LONG_NAME[:41] + "_" + hashlib.sha256(LONG_NAME.encode()).hexdigest()[:8]
TOOLS = [{"type": "function", "function": {"name": name, "description": "Public probe",
          "parameters": {"type": "object", "properties": {}}}}
         for name in ("probe", "probe2", LONG_NAME, "skill_view")]
SCENARIOS = {"S1": "per-call", "S2": "warm alive", "S3": "idle restart",
             "S4": "LRU restart", "S5": "gateway restart", "S6": "review fork",
             "S7": "model swap and back", "S8": "restart after first turn"}
SYSTEM = "PUBLIC SYNTHETIC SYSTEM\n" * 300
PUBLIC_EMAIL = "public@example.invalid"
PUBLIC_UUID = "00000000-0000-0000-0000-000000000000"
EMAIL_REMINDER = "The user's email address is"


class Peer(WirePeer):
    """The base SSE peer with a per-create script and distinct thinking signatures."""

    def do_GET(self):
        self.server.paths.append({"method": "GET", "path": self.path.split("?")[0]})
        self.send_error(501, "Unsupported method ('GET')")

    def do_POST(self):
        path = self.path.split("?")[0]
        self.server.paths.append({"method": "POST", "path": path})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if path == "/v1/messages/count_tokens":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        if path != "/v1/messages":
            self.send_error(403)
            return
        self.server.wires.append(body)
        number = len(self.server.wires)
        count = self.server.tool_count
        offered = {tool["name"].split("__")[-1]: tool["name"] for tool in body["tools"]}
        names = (LONG_ALIAS,) if self.server.long_tool else ("probe", "probe2")[:count]
        blocks = ([{"type": "thinking", "thinking": f"PUBLIC SYNTHETIC THINKING {number}",
                    "signature": f"public-fixture-signature-{number}"}] + [
            {"type": "tool_use", "id": f"toolu_public_{number}_{i}",
             "name": offered[name], "input": {}}
            for i, name in enumerate(names)]) if count else [
                {"type": "text", "text": f"PUBLIC SYNTHETIC ANSWER {number}"}]
        stop = "tool_use" if count else "end_turn"
        message = {"id": f"msg_public_{number}", "type": "message", "role": "assistant",
                   "model": body["model"], "content": [], "stop_reason": None,
                   "stop_sequence": None, "usage": USAGE}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def emit(event):
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

        emit({"type": "message_start", "message": message})
        for index, block in enumerate(blocks):
            kind = block["type"]
            start = ({"type": kind, "thinking": "", "signature": ""} if kind == "thinking"
                     else {**block, "input": {}} if kind == "tool_use" else {"type": kind, "text": ""})
            emit({"type": "content_block_start", "index": index, "content_block": start})
            deltas = ([{"type": "thinking_delta", "thinking": block["thinking"]},
                       {"type": "signature_delta", "signature": block["signature"]}] if kind == "thinking"
                      else [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
                      if kind == "tool_use" else [{"type": "text_delta", "text": block["text"]}])
            for delta in deltas:
                emit({"type": "content_block_delta", "index": index, "delta": delta})
            emit({"type": "content_block_stop", "index": index})
        emit({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": USAGE})
        emit({"type": "message_stop"})


def blocks(value):
    return [{"type": "text", "text": value}] if isinstance(value, str) else value


def block_receipt(block):
    if block is None:
        return None
    text = "".join(n.get("text", n.get("thinking", "")) for n in nodes(block))
    return {"type": block.get("type", "metadata"), "text_length": len(text),
            "sha256": digest(block)[:12]}


def divergence(old, new):
    """First differing header/message/block; never return raw synthetic content."""
    for section in ("tools", "system", "messages"):
        left, right = old.get(section, []), new.get(section, [])
        for i in range(max(len(left), len(right))):
            a = left[i] if i < len(left) else None
            b = right[i] if i < len(right) else None
            if a == b:
                continue
            receipt = {"section": section, "message_index": i if section == "messages" else None,
                       "header_index": i if section != "messages" else None}
            if section == "messages":
                ac, bc = blocks(a.get("content", [])) if a else [], blocks(b.get("content", [])) if b else []
                if a is None or b is None or a.get("role") != b.get("role"):
                    return {**receipt, "block_index": None, "old": block_receipt(a), "new": block_receipt(b)}
                for j in range(max(len(ac), len(bc))):
                    ab, bb = ac[j] if j < len(ac) else None, bc[j] if j < len(bc) else None
                    if ab != bb:
                        return {**receipt, "block_index": j, "old": block_receipt(ab), "new": block_receipt(bb)}
            return {**receipt, "block_index": None, "old": block_receipt(a), "new": block_receipt(b)}
    return None


class WarmLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("warm "):
            self.events.append(message)


def isolated_env(tmp, port, warm, subscription=False):
    # Native prefetches macOS credentials even with an injected token. Shadow
    # its PATH-resolved `security` command so no real keychain item is read.
    commands = Path(tmp) / "bin"
    commands.mkdir(exist_ok=True)
    security = commands / "security"
    security.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    security.chmod(0o700)
    env = {"PATH": os.defpath, "HOME": tmp, "HERMES_HOME": tmp,
           "CLAUDE_CONFIG_DIR": str(Path(tmp) / "config"), "XDG_CONFIG_HOME": tmp,
           "ANTHROPIC_API_KEY": "sk-ant-public-offline-fixture",
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
           "DISABLE_ERROR_REPORTING": "1", "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "CLAUDE_SUBSCRIPTION_DIRECTSDK_WARM": str(int(warm))}
    env.update(PATH=str(commands) + os.pathsep + os.defpath, TMPDIR=tempfile.gettempdir())
    if subscription:
        env.pop("ANTHROPIC_API_KEY")
        # Tn() reads this cached account only when hl() selects OAuth. The
        # injected token defaults to inference-only scopes, so native needs no
        # profile fetch, refresh token or credential-store fixture.
        env.update(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-public-offline-fixture",
                   CLAUDE_CODE_SUBSCRIPTION_TYPE="max")
        config = Path(env["CLAUDE_CONFIG_DIR"])
        config.mkdir(exist_ok=True)
        (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "oauthAccount": {"accountUuid": PUBLIC_UUID, "emailAddress": PUBLIC_EMAIL,
                             "organizationUuid": PUBLIC_UUID}}), encoding="utf-8")
    env.update({key: "http://127.0.0.1:1" for key in
                ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")})
    return env


def scenario(native, binary, model, name, log, subscription=False):
    warm = name != "S1"
    report = {"pass": True, "name": SCENARIOS[name], "boundaries": [], "requests": 0,
              "main_requests": 0, "mismatches": 0, "system_messages": [], "kills": [], "receipts": []}
    previous, history = {}, [{"role": "system", "content": SYSTEM}]
    idle, size = getattr(native, "WARM_IDLE_SECONDS", None), getattr(native, "WARM_POOL_SIZE", None)
    if warm:
        native._warm_shutdown()
        native.WARM_IDLE_SECONDS = 1.0 if name == "S3" else idle
        native.WARM_POOL_SIZE = 1 if name == "S4" else size
    log_start = len(log.events)
    with tempfile.TemporaryDirectory(prefix=f"cache-continuity-{name}-") as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Peer) as peer:
        peer.wires, peer.tool_count, peer.paths = [], 0, []
        worker = threading.Thread(target=peer.serve_forever, daemon=True)
        worker.start()
        env = isolated_env(tmp, peer.server_port, warm, subscription)

        def new_client():
            return native.Client(command=str(binary), env=env, timeout=45)

        client = new_client()

        def request(messages, chosen_model, turn, round_number, tool_count, main=True):
            peer.tool_count = tool_count
            peer.long_tool = main and round_number == 2
            original, start, logs = copy.deepcopy(messages), len(peer.wires), len(log.events)
            result = client.create(model=chosen_model, messages=messages, tools=TOOLS)
            wires = peer.wires[start:]
            events = log.events[logs:]
            modes = [re.search(r" mode=(\S+)", e).group(1) for e in events if e.startswith("warm step:")]
            mode = modes[-1] if modes else "warm-fallback" if warm else "per-call"
            expected = Counter(digest(content(n)) for n in nodes(original) if n.get("type") == "thinking")
            for index, wire in enumerate(wires):
                markers = [(i, j) for i, msg in enumerate(wire["messages"]) if isinstance(msg["content"], list)
                           for j, b in enumerate(msg["content"]) if "cache_control" in b]
                marker_count = sum("cache_control" in n for n in nodes(wire))
                prompt = content({key: wire[key] for key in ("tools", "system", "messages")})
                actual = Counter(digest(n) for n in nodes(prompt["messages"]) if n.get("type") == "thinking")
                replay = not (expected - actual)
                receipt = {"turn": turn, "round": round_number, "main": main, "model": chosen_model,
                           "upstream_index": start + index + 1, "mode": mode, "match": None,
                           "markers": markers, "marker_count": marker_count,
                           "message_marker_count": len(markers), "thinking_expected": sum(expected.values()),
                           "thinking_replay": replay, "continuity_checked": main,
                           "history_unchanged": messages == original,
                           "account_email_reminder": any(EMAIL_REMINDER in n.get("text", "")
                                                         for n in nodes(wire)),
                           "upstream_requests": len(wires)}
                # S7's first sonnet request starts a new model cache; returning to
                # opus must still extend the last prefix opus actually wrote.
                exempt = main and name == "S7" and turn == 3 and round_number == 1
                receipt["exempt"] = exempt
                if main and chosen_model in previous and not exempt:
                    old, at, old_turn = previous[chosen_model]
                    try:
                        candidate = cut(prompt, at)
                    except (IndexError, TypeError):
                        candidate = prompt
                    receipt.update(match=candidate == old, compared_to_turn=old_turn,
                                   old_marker=at, old_prefix_sha256=digest(old),
                                   replayed_prefix_sha256=digest(candidate))
                    if not receipt["match"]:
                        receipt["divergence"] = divergence(old, candidate)
                        report["mismatches"] += 1
                if main and markers:
                    previous[chosen_model] = cut(prompt, markers[-1]), markers[-1], turn
                # Auxiliary requests retain observations, but the spec's prefix,
                # marker and thinking checks apply to the main conversation only.
                valid = len(wires) == 1 and messages == original
                if main:
                    valid = (valid and replay and marker_count <= 4
                             and 1 <= len(markers) <= 2 and receipt["match"] is not False)
                if warm:
                    valid = valid and len(modes) == 1 and "warm-fallback" != mode
                receipt["valid"] = valid
                report["pass"] &= valid
                report["receipts"].append(receipt)
                if main and round_number == 1:
                    boundary = {k: receipt[k] for k in ("turn", "match", "mode", "exempt")}
                    for k in ("divergence", "compared_to_turn", "old_prefix_sha256", "replayed_prefix_sha256"):
                        if k in receipt:
                            boundary[k] = receipt[k]
                    if name == "S3" or (turn > 1 and name in ("S4", "S5")) or (name == "S8" and turn == 2):
                        boundary["mode_expected"] = "cold or rebuild:*"
                        boundary["mode_ok"] = mode == "cold" or mode.startswith("rebuild:")
                    elif (name in ("S2", "S6") and turn > 1) or (name == "S8" and turn > 2):
                        boundary["mode_expected"], boundary["mode_ok"] = "reused", mode == "reused"
                    report["pass"] &= boundary.get("mode_ok", True)
                    report["boundaries"].append(boundary)
                    if name == "S8" and turn == 2:
                        report["turn_2_boundary"] = boundary
                for i, msg in enumerate(wire["messages"]):
                    if msg.get("role") == "system":
                        value = msg["content"]
                        text = value if isinstance(value, str) else "".join(b.get("text", "") for b in value)
                        report["system_messages"].append({"turn": turn, "round": round_number,
                            "main": main, "upstream_index": start + index + 1, "message_index": i,
                            "length": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()[:12]})
            if not wires:
                report["pass"] = False
                report["receipts"].append({"turn": turn, "round": round_number, "main": main,
                                           "upstream_requests": 0, "history_unchanged": messages == original})
            assistant = result.choices[0].message.model_dump()
            # Also check that the scripted assistant really traversed the tool path.
            if len(assistant.get("tool_calls") or []) != tool_count:
                report["pass"] = False
                report.setdefault("shape_errors", []).append({"turn": turn, "round": round_number,
                    "expected_tool_calls": tool_count, "actual_tool_calls": len(assistant.get("tool_calls") or [])})
            if peer.long_tool:
                restored = [c["function"]["name"] for c in assistant.get("tool_calls") or []] == [LONG_NAME]
                report.setdefault("long_tool_round_trips", []).append({"turn": turn, "pass": restored})
                report["pass"] &= restored
            return assistant

        try:
            for turn in range(1, 7):
                if turn > 1:
                    if name == "S3":
                        time.sleep(1.5)
                    if name in ("S5",) or (name == "S8" and turn == 2):
                        native._warm_shutdown()
                        client.close()
                        client = new_client()
                    if name == "S4":
                        request([{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": f"Unrelated public conversation {turn}"}],
                                model, turn, 0, 0, main=False)
                    if name == "S6" and turn == 4:
                        request(copy.deepcopy(history) + [{"role": "user", "content": "Review the above."}],
                                "claude-sonnet-5", turn, 0, 0, main=False)
                history.append({"role": "user", "content": f"Public main question {turn}: run the probes."})
                chosen_model = "claude-sonnet-5" if name == "S7" and turn in (3, 4) else model
                for round_number, tool_count in enumerate((2, 1, 1, 0), 1):
                    assistant = request(history, chosen_model, turn, round_number, tool_count)
                    history.append(assistant)
                    history.extend({"role": "tool", "tool_call_id": call["id"],
                                    "content": (f"Public synthetic result turn {turn} round {round_number}\n" * 100)[:2048]}
                                   for call in assistant.get("tool_calls") or [])
                print(f"{name} turn {turn}/6: mismatches={report['mismatches']}", file=sys.stderr, flush=True)
        except Exception as error:
            report.update({"pass": False, "stopped": True,
                           "error": {"type": type(error).__name__, "message": str(error)}})
        finally:
            client.close()
            if warm:
                native._warm_shutdown()
                native.WARM_IDLE_SECONDS, native.WARM_POOL_SIZE = idle, size
            peer.shutdown()
            worker.join(timeout=5)
            report["requests"] = len(peer.wires)
            report["account_email_reminder_requests"] = sum(
                any(EMAIL_REMINDER in n.get("text", "") for n in nodes(wire)) for wire in peer.wires)
            if subscription:
                report["subscription_emulated"] = report["account_email_reminder_requests"] > 0
                report["pass"] &= report["subscription_emulated"]
            report["peer_paths"] = peer.paths
            report["main_requests"] = sum(r["main"] for r in report["receipts"])
            report["kills"] = [e for e in log.events[log_start:] if e.startswith("warm kill:")]
            report["fallbacks"] = [e for e in log.events[log_start:] if e.startswith("warm fallback:")]
    return report


def run_isolated(args):
    root, binary = args.root.resolve(), args.binary.resolve()
    subscription = getattr(args, "subscription", False)
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("cache_continuity_directsdk", root / "directsdk.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    log = WarmLog()
    logger = logging.getLogger(native.__name__)
    logger.addHandler(log)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Even --version runs with a fake HOME; no real CLI config is consulted.
    with tempfile.TemporaryDirectory(prefix="cache-continuity-version-") as tmp:
        version = subprocess.check_output([str(binary), "--version"], env=isolated_env(tmp, 1, False),
                                          stdin=subprocess.DEVNULL, text=True, timeout=15).strip()
    output = {"native_version": version, "native_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "root": str(root), "model": args.model, "eval_version": "B0b" if subscription else "B0",
              "subscription": subscription,
              "synthetic_usage_not_cache_measurement": True, "scenarios": {}}
    for name in args.only:
        if name != "S1" and not hasattr(native, "_warm_checkout"):
            output["scenarios"][name] = {"pass": None, "skipped": "root has no warm mode"}
            continue
        try:
            output["scenarios"][name] = scenario(native, binary, args.model, name, log, subscription)
        except OSError as error:
            output["scenarios"][name] = {"pass": False, "stopped": True,
                "error": {"type": type(error).__name__, "message": str(error)}}
        if output["scenarios"][name].get("stopped"):
            break  # Binding/spawn/runtime failures are not grounds for a workaround.
    logger.removeHandler(log)
    return output


def run(args):
    # Setup helpers may read Hermes settings in the Python process itself, before
    # spawning native. Isolate that process too, not just Client's child env.
    with tempfile.TemporaryDirectory(prefix="cache-continuity-runner-") as tmp:
        saved = dict(os.environ)
        env = isolated_env(tmp, 1, False)
        env.update(TMPDIR=tempfile.gettempdir(), HERMES_DISABLE_LAZY_INSTALLS="1")
        os.environ.clear()
        os.environ.update(env)
        try:
            return run_isolated(args)
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--binary", type=Path, default=Path("/Users/m1/.local/share/claude/versions/2.1.299"))
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--subscription", action="store_true",
                        help="emulate a subscription login offline using public account fixtures")
    parser.add_argument("--only", default=",".join(SCENARIOS), help="comma-separated scenario ids, e.g. S3,S5")
    args = parser.parse_args()
    args.only = args.only.split(",")
    if not args.only or any(name not in SCENARIOS for name in args.only):
        parser.error("--only must select S1 through S8")
    result = run(args)
    print(json.dumps(result, indent=2))
    sys.exit(int(any(s["pass"] is False for s in result["scenarios"].values())))
