#!/usr/bin/env python3
"""Direct model calls to 9router, bypassing the subagent model registry.

WHY THIS EXISTS
---------------
OMP subagents resolve their `model:` chain against a model registry that is
built once and never refreshed (log: "runSubagent: reusing parent modelRegistry;
skipping refresh"). Deleting `~/.omp/agent/models.db` did not help — the
registry lives in the parent process.

Consequence: any provider added to 9router AFTER the session started is
invisible to subagents. Measured 2026-10-03: 9router serves 20 zanslab models,
but subagents can only ever reach the models that were present at session start
(`ag/*`, `cbai/*`, `oc/*`). Chains pointing at `zanslab/*`, `vs/*` or `AG/*`
silently skip those entries.

So: for models the owner explicitly wants (zanslab `op/gpt-6-sol` and
`op/gpt-6-luna`), call them HERE, from the orchestrator, not through a subagent.

USAGE
-----
    python3 tools/direct_model.py --model zanslab/op/gpt-6-sol --prompt "..." 
    python3 tools/direct_model.py --model zanslab/op/gpt-6-luna --file task.md
    python3 tools/direct_model.py --list                     # what is reachable
    python3 tools/direct_model.py --model ... --json         # machine readable

Language note: AgentRouter (`AG/*`) rejects Indonesian prompts with HTTP 400
`content-blocked` — 4 models verified. zanslab, Antigravity (`ag/*`) and vsllm
(`vs/*`) accept Indonesian. See docs/architecture/15-provider-language-limits.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROUTER_DB = Path.home() / ".9router" / "db" / "data.sqlite"
BASE_URL = "http://127.0.0.1:20128/v1/chat/completions"
MODELS_URL = "http://127.0.0.1:20128/v1/models"


def api_key() -> str:
    """Read the active 9router API key from its database."""

    con = sqlite3.connect(str(ROUTER_DB))
    try:
        row = con.execute(
            "SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit("no active 9router API key found")
    return row[0]


def parse_response(raw: str) -> dict:
    """9router returns plain JSON, concatenated JSON, or SSE. Handle all three.

    A bare json.loads() fails on two of the three shapes, and the failure looks
    like "the model returned nothing" — which cost a whole session of wrong
    benchmarks once. See tools/router_probe.py for the long version.
    """

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        return obj
    except Exception:
        pass
    merged: dict = {}
    usage: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            frame = json.loads(payload)
        except Exception:
            continue
        choices = frame.get("choices") or [{}]
        choice = choices[0] if choices else {}
        for key in ("delta", "message"):
            piece = (choice.get(key) or {}).get("content")
            if piece:
                merged["content"] = merged.get("content", "") + piece
        if frame.get("usage"):
            usage = frame["usage"]
        if frame.get("model"):
            merged["model"] = frame["model"]
    merged["usage"] = usage
    return merged


def call(model: str, prompt: str, *, max_tokens: int = 4000, timeout: int = 300) -> dict:
    """Call one model. Returns a dict with content, usage and timing."""

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        BASE_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key()}",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        return {
            "ok": False,
            "error": f"HTTP {exc.code}",
            "detail": detail,
            "duration_s": round(time.perf_counter() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc)[:200],
            "duration_s": round(time.perf_counter() - started, 2),
        }

    parsed = parse_response(raw)
    choices = parsed.get("choices") or [{}]
    choice = choices[0] if choices else {}
    content = (parsed.get("content") or "").strip()
    if not content:
        content = ((choice.get("message") or {}).get("content") or "").strip()
    usage = parsed.get("usage") or {}
    return {
        "ok": bool(content),
        "content": content,
        "model_returned": parsed.get("model", ""),
        "finish_reason": choice.get("finish_reason", ""),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens", 0
        ),
        "completion_tokens": usage.get("completion_tokens", 0),
        "duration_s": round(time.perf_counter() - started, 2),
    }


def list_models() -> None:
    """Print every model 9router currently serves, grouped by prefix."""

    request = urllib.request.Request(
        MODELS_URL, headers={"Authorization": f"Bearer {api_key()}"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode())
    models = payload.get("data", payload if isinstance(payload, list) else [])
    ids = sorted(
        m.get("id", "") for m in models if isinstance(m, dict)
    )
    groups: dict[str, list[str]] = {}
    for mid in ids:
        groups.setdefault(mid.split("/")[0], []).append(mid)
    print(f"9router serves {len(ids)} models across {len(groups)} prefixes\n")
    for prefix in sorted(groups):
        print(f"  {prefix} ({len(groups[prefix])})")
        for mid in groups[prefix][:40]:
            print(f"    {mid}")
        if len(groups[prefix]) > 40:
            print(f"    ... and {len(groups[prefix]) - 40} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", help="model id, e.g. zanslab/op/gpt-6-sol")
    parser.add_argument("--prompt", help="prompt text")
    parser.add_argument("--file", help="read the prompt from this file")
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--list", action="store_true", help="list reachable models")
    args = parser.parse_args(argv)

    if args.list:
        list_models()
        return 0

    if not args.model:
        parser.error("--model is required unless --list is used")

    if args.file:
        prompt = Path(args.file).read_text(encoding="utf-8")
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        parser.error("empty prompt")

    result = call(
        args.model, prompt, max_tokens=args.max_tokens, timeout=args.timeout
    )

    if args.json:
        print(json.dumps(result, indent=1))
    elif result["ok"]:
        print(result["content"])
        print(
            f"\n--- {args.model} | {result['duration_s']}s | "
            f"{result['completion_tokens']} tok out | "
            f"{result['reasoning_tokens']} reasoning ---",
            file=sys.stderr,
        )
    else:
        print(f"FAILED: {result['error']}", file=sys.stderr)
        print(result.get("detail", ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
