#!/usr/bin/env python3
"""Flowmax agent skill — manage YOUR agents on the Hubble Market Server via JWT.

Audience: non-developer users, driven by Claude through natural-language interview.
The user picks high-level options (symbol / goal / style / public?); this script
does the mechanical work: probe legal values, create mock credentials, assemble
canonical prompts/risk-config, poll deploy, and read back logs / PnL / positions.

Scope (what the user needs):
  - create research agent / pm agent
  - view trading logs (PnL summary, per-order PnL, open positions)
  - view agent info (list / status / research deploy progress)
  - plus the minimum operate-side (start/stop/delete) so a created PM actually runs
    and a mistaken one can be removed.

Security:
  - JWT read from env FLOWMAX_JWT (fallback HUBBLE_JWT / JWT). NEVER hardcoded or
    printed (only a masked prefix). Users set it in their own shell — never paste
    it into chat.
  - BASE from env FLOWMAX_BASE (fallback HUBBLE_BASE), default staging
    https://market.dev.gcp.hubble-rpc.xyz . prod = https://market.prod.gcp.hubble-rpc.xyz .

Stdlib only — runs with plain `python3` (no uv/deps). Canonical PM templates
(goal prompt segments / risk tiers / interval map / exit policies) are frozen in
this file so the skill is independent of any repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_PREFIX = "/api/v1"
DEFAULT_BASE = "https://market.dev.gcp.hubble-rpc.xyz"  # staging (host is "dev", not "staging")
PROD_BASE = "https://market.prod.gcp.hubble-rpc.xyz"


# =========================================================================
# Frozen canonical templates (mirror src/app/services/quick_create_service.py).
# Kept in-repo-file so the skill has no repo dependency. Refresh by re-extracting
# from quick_create_service.py if the marketplace wizard templates change.
# =========================================================================

DEFAULT_INTERVAL_MAP = {
    "steady": 3_600_000,      # 1h
    "swing": 900_000,         # 15m
    "trend": 900_000,         # 15m
    "arbitrage": 3_600_000,   # 1h
}

RISK_TIERS = {
    "conservative": {
        "leverage_range": [1, 1],
        "max_total_margin_pct": 0.30,
        "max_single_token_margin_pct": 0.15,
        "max_net_exposure_pct": 0.20,
    },
    "balanced": {
        "leverage_range": [1, 3],
        "max_total_margin_pct": 0.50,
        "max_single_token_margin_pct": 0.20,
        "max_net_exposure_pct": 0.30,
    },
    "aggressive": {
        "leverage_range": [1, 5],
        "max_total_margin_pct": 0.80,
        "max_single_token_margin_pct": 0.25,
        "max_net_exposure_pct": 0.50,
    },
}

PROMPT_SEGMENTS = {
    "steady": """你是稳健增值型 PM Agent，遵循以下原则：
- 风险控制优先于收益，单笔损失严格控制在 5% 以内
- 优先选择慢牛、温和上行、有底部支撑的行情
- 严格止损、低杠杆、长期持有，避免频繁交易
- 决策频率低（默认 1 小时一次），动作克制（0-1 笔/天为常态）
- 月度目标收益 3-5%，最大回撤控制在 5% 以内
- 不追逐热点，不参与 Meme 币炒作，不做高杠杆投机
- 收到矛盾信号时优先空仓观望，宁可错过不可做错""",
    "swing": """你是波段套利型 PM Agent，遵循以下原则：
- 在明确的支撑阻力区间内高抛低吸，捕捉中短期波动
- 优先选择宽幅震荡、规律来回、有明确高低点的行情
- 严格按区间纪律交易，触阻力即减仓、触支撑即加仓
- 突破行情止损跟进，不与趋势对抗
- 月度目标收益 5-15%（视风格档位），最大回撤控制在 10% 以内
- 决策频率中等（默认 15 分钟一次）
- 资金费率、技术面、链上数据综合判断，避免单一指标误导""",
    "trend": """你是趋势跟踪型 PM Agent，遵循以下原则：
- 顺势而为，确认趋势后再进场，绝不逆势抄底摸顶
- 突破跟进：价格放量突破关键位时果断入场
- 追踪止损（trailing stop）保护已有利润，让利润奔跑
- 趋势结束信号明确时果断离场，不抱幻想
- 月度目标收益 10-50%（视风格档位），最大回撤接受 15-30%
- 决策频率中等（默认 15 分钟一次），但持仓周期可长达数日
- 链上数据 + 技术面 + 舆情综合判断趋势强度
- 震荡市中保持克制，频繁假突破时降低仓位或观望""",
    "arbitrage": """你是套利对冲型 PM Agent，遵循以下原则：
- 不赌方向，赚取结构性价差（资金费率、期现基差、跨市场价差）
- 优先 Delta 中性策略，对冲掉方向性风险
- 任何行情都可参与，最不挑行情的策略类型
- 月度目标收益 1-5%（视风格档位），最大回撤控制在 1-5% 以内
- 决策频率低（默认 1 小时一次），动作克制
- 严控对冲腿的滑点和手续费，价差不足时果断放弃
- 资金费率 + 链上数据是主要信号源
- 不演变为方向性投机，发现对冲腿失效立即平仓""",
}

EXIT_POLICIES = {
    "take_quick": "止盈止损偏好：见好就收 —— 快进快出，止盈 +2% / 止损 -1.5%，积小胜为大胜，频繁落袋。",
    "disciplined": "止盈止损偏好：纪律为王 —— 严格按计划，止盈 +5% / 止损 -3%，不被情绪左右，纪律高于一切。",
    "diamond": "止盈止损偏好：钻石手 —— 拿得住才赚得多，止盈 +12% / 止损 -8%，容忍短期回撤，博取大趋势。",
}

# goal×style pairs that contradict each other (only warn, not blocked).
CONFLICT_PAIRS = {("steady", "aggressive"), ("arbitrage", "aggressive")}

# Optional prompt overlays appended after the goal segment.
OVERLAYS = {
    # Recommended risk-control overlay for any real trading PM.
    "hard-stop": """【硬止损纪律（最高优先级，高于一切加仓/补仓/调仓指令）】
- 单笔交易最大亏损 = 账户本金的 2%。开仓时必须按止损距离反推仓位，确保触发止损时亏损 ≤ 2% 本金。
- 持仓浮亏达到本金 2% 必须立即平仓，不得拖延、不得下移止损、不得加仓摊薄成本。
- 禁止对亏损仓位加仓（禁止马丁格尔/摊平）；只有已盈利仓位才可在保本前提下追加。
- 任一持仓被止损后，当轮不得在同一方向重新进场。""",
    # WARNING: historical "multi-trade" overlay. Prod ops found this CAUSES
    # position piling (12-125 open positions) because it overrides the restraint
    # clauses. Do NOT use for real trading PMs. Kept only for reproducibility.
    "multi-trade": """【用户偏好覆盖】
- 多交易：每个决策周期尽量产生动作（开仓/平仓/调仓/反手），不允许长时间空仓或持仓不动
- 多有交易行为：把"保持交易活跃度"作为优先目标，遇到任何可执行信号立即响应
- 频繁调仓：根据最新研究信号动态调整仓位、方向、杠杆，宁可小幅频繁调整也不要等待所谓"完美点位"
- 以上偏好优先级高于策略段中"动作克制/决策频率低/0-1 笔/天"等克制类约束""",
}

GOAL_LABELS = {
    "steady": "稳健保值", "swing": "波段", "trend": "趋势", "arbitrage": "套利",
}
STYLE_LABELS = {
    "conservative": "保守", "balanced": "平衡", "aggressive": "激进",
}


# =========================================================================
# config + http
# =========================================================================
def base_url() -> str:
    return os.environ.get("FLOWMAX_BASE") or os.environ.get("HUBBLE_BASE") or DEFAULT_BASE


def env_short(name: str) -> str:
    v = os.environ.get(name, "")
    return f"{v[:6]}…(len={len(v)})" if v else "(unset)"


def get_jwt() -> str:
    t = os.environ.get("FLOWMAX_JWT") or os.environ.get("HUBBLE_JWT") or os.environ.get("JWT")
    if not t:
        sys.stderr.write(
            "ERROR: set FLOWMAX_JWT env var to your login JWT first (from the website after "
            "login — localStorage / cookie). Never paste it into chat; export it in your own "
            "shell, e.g.  export FLOWMAX_JWT=\"<token>\"\n"
        )
        sys.exit(2)
    return t


def api(method: str, path: str, body=None, quiet: bool = False, prefix: str = API_PREFIX):
    """Return (status_code, parsed_json_or_text_or_None). Errors -> stderr unless quiet."""
    url = base_url().rstrip("/") + prefix + path
    data = None
    headers = {"Authorization": f"Bearer {get_jwt()}"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            if not raw:
                return r.status, None
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw[:300]
        if not quiet:
            sys.stderr.write(f"  {method} {path} -> HTTP {e.code}: {detail}\n")
        return e.code, detail
    except urllib.error.URLError as e:
        if not quiet:
            sys.stderr.write(f"  {method} {path} -> URLError: {e}\n")
        return 0, str(e)


# =========================================================================
# helpers
# =========================================================================
def find_mock_system_auth_id():
    c, d = api("GET", "/system-exchange-auths", quiet=True)
    if c != 200 or not isinstance(d, list):
        return None
    for x in d:
        if x.get("exchange_key") == "mock":
            return x.get("id")
    return None


def default_llm():
    """Probe enabled llm provider/model; fall back to deepseek/deepseek-v4-flash."""
    c, d = api("GET", "/config/llm-providers", quiet=True)
    if c == 200 and isinstance(d, list) and d:
        first = d[0]
        pid = first.get("id") or first.get("provider_id")
        m = first.get("model") or first.get("models")
        if isinstance(m, list) and m:
            m = m[0]
        return pid or "deepseek", m or "deepseek-v4-flash"
    return "deepseek", "deepseek-v4-flash"


def build_pm_prompt(goal: str, exit_policy=None, overlay=None, extra_prompt=None) -> str:
    parts = [PROMPT_SEGMENTS[goal]]
    if exit_policy and exit_policy in EXIT_POLICIES:
        parts.append(EXIT_POLICIES[exit_policy])
    if overlay == "multi-trade":
        sys.stderr.write(
            "  WARN: overlay 'multi-trade' causes position piling in production. "
            "Use 'hard-stop' for real trading PMs.\n"
        )
    if overlay and overlay in OVERLAYS:
        parts.append(OVERLAYS[overlay])
    if extra_prompt:
        parts.append(extra_prompt)
    return "\n\n".join(parts)


def pretty(d) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2) if d is not None else "(empty)"


# =========================================================================
# probe
# =========================================================================
def cmd_probe(args):
    print(f"BASE = {base_url()}")
    print(f"JWT  = {env_short('FLOWMAX_JWT') or env_short('HUBBLE_JWT') or env_short('JWT')}")
    print("\n[asset_types]")
    c, d = api("GET", "/research/asset-types")
    print(pretty(d) if d else f"HTTP {c}")

    print("\n[llm-providers]  (enabled values — probe every run, do not hardcode)")
    c, d = api("GET", "/config/llm-providers")
    print(pretty(d) if d else f"HTTP {c}")

    if args.asset_type:
        q = urllib.parse.quote(args.asset_type)
        print(f"\n[research/config?asset_type={args.asset_type}]")
        c, d = api("GET", f"/research/config?asset_type={q}")
        if isinstance(d, dict) and d.get("datasources"):
            for x in d["datasources"]:
                print(f"  {str(x.get('id')):30} | {str(x.get('analysis_type')):26} | {x.get('name')}")
        else:
            print(pretty(d) if d else f"HTTP {c}")

    mock = find_mock_system_auth_id()
    print(f"\n[mock system_exchange_auth_id]  {mock or '(not found — PM create will fail)'}")

    print("\n[embedded canonical presets]")
    print(f"  goals    : {' / '.join(f'{g}({GOAL_LABELS[g]})' for g in PROMPT_SEGMENTS)}")
    print(f"  styles   : {' / '.join(f'{s}({STYLE_LABELS[s]})' for s in RISK_TIERS)}")
    print(f"  intervals: {DEFAULT_INTERVAL_MAP}   (skill default for new PMs = 900000 = 15min)")
    print(f"  exit     : {list(EXIT_POLICIES)}")
    print(f"  overlays : {list(OVERLAYS)}   (recommend 'hard-stop' for real trading PMs)")
    print(f"  conflict pairs (warn only): {sorted(CONFLICT_PAIRS)}")
    return 0


# =========================================================================
# research
# =========================================================================
def _wait_research(job_id, max_wait, poll):
    deadline = time.monotonic() + max_wait
    last = None
    while time.monotonic() < deadline:
        c, d = api("GET", f"/agents/user-research/jobs/{job_id}", quiet=True)
        if isinstance(d, dict):
            last = d.get("status")
            if last in ("succeeded", "failed", "deployed", "error"):
                return last
        time.sleep(poll)
    return last or "timeout"


def cmd_research_create(args):
    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
        if isinstance(spec, dict):
            spec = [spec]
    else:
        # single item from individual flags
        spec = [{
            "name": args.name,
            "prompt": args.prompt,
            "asset_type": args.asset_type,
            "analysis_type": args.analysis_type,
            "language": args.language,
            "is_public": args.public,
        }]
        if args.description:
            spec[0]["description"] = args.description

    pid, model = default_llm()
    results = []
    for i, item in enumerate(spec):
        item.setdefault("llm_provider_id", pid)
        item.setdefault("llm_model", model)
        item.setdefault("asset_type", "Crypto")
        item.setdefault("language", "zh-CN")
        # NEVER send datasource_ids — Creator reconciles from prompt automatically.
        item.pop("datasource_ids", None)
        name = item.get("name", f"research-{i}")
        print(f"[{i + 1}/{len(spec)}] create research: {name}")
        c, d = api("POST", "/agents/user-research", body=item)
        if c not in (200, 201, 202) or not isinstance(d, dict):
            results.append({"name": name, "ok": False, "status": c, "error": d})
            time.sleep(args.interval)
            continue
        rec = {"name": name, "ok": True,
               "agent_id": d.get("agent_id"), "job_id": d.get("job_id"), "status": d.get("status")}
        if args.wait > 0 and rec["job_id"]:
            rec["deploy_status"] = _wait_research(rec["job_id"], args.wait, args.poll)
        results.append(rec)
        time.sleep(args.interval)

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"\nwrote {len(results)} results -> {args.out}")
    print("\nresearch agent_ids:")
    for r in results:
        flag = "OK  " if r.get("ok") else "FAIL"
        tail = r.get("deploy_status") or r.get("status") or str(r.get("error"))
        print(f"  {flag} {str(r.get('name')):30} {r.get('agent_id')}  {tail}")
    return 0 if all(r.get("ok") for r in results) else 1


def cmd_research_get(args):
    c, d = api("GET", f"/agents/user-research/{args.agent}")
    print(pretty(d) if d else f"HTTP {c}")
    if isinstance(d, dict):
        cv = (d.get("current_version_info") or {})
        ds = cv.get("datasource_ids")
        if ds is not None:
            print(f"\nreconciled datasource_ids ({len(ds) if isinstance(ds, list) else '?'}): {ds}")
    return 0 if c == 200 else 1


def cmd_research_job(args):
    c, d = api("GET", f"/agents/user-research/jobs/{args.job}")
    print(pretty(d) if d else f"HTTP {c}")
    return 0 if c == 200 else 1


# =========================================================================
# pm
# =========================================================================
def cmd_pm_create(args):
    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
    else:
        spec = {
            "name": args.name,
            "symbols": [s.strip().upper() for s in args.symbol.split(",") if s.strip()],
            "goal": args.goal,
            "style": args.style,
            "interval_ms": args.interval,
            "is_public": args.public,
            "auto_start_scheduler": args.auto_start,
        }
        if args.research_ids:
            spec["research_agent_ids"] = [s.strip() for s in args.research_ids.split(",") if s.strip()]
        if args.exit_policy:
            spec["exit_policy"] = args.exit_policy
        if args.overlay:
            spec["overlay"] = args.overlay
        if args.extra_prompt:
            spec["extra_prompt"] = args.extra_prompt

    goal = spec.get("goal")
    style = spec.get("style", "balanced")
    if not goal or goal not in PROMPT_SEGMENTS:
        sys.stderr.write(f"ERROR: --goal required, one of {list(PROMPT_SEGMENTS)}\n")
        return 2
    if (goal, style) in CONFLICT_PAIRS:
        sys.stderr.write(f"  WARN: ({goal},{style}) is a contradiction pair — proceeds, but behavior may be incoherent.\n")

    sys_prompt = spec.get("system_prompt") or build_pm_prompt(
        goal,
        exit_policy=spec.get("exit_policy"),
        overlay=spec.get("overlay"),
        extra_prompt=spec.get("extra_prompt"),
    )
    risk = spec.get("risk_config") or RISK_TIERS[style]
    pid, model = default_llm()

    # mock credential (paper trading — one fresh mock auth per PM)
    mock_sys = find_mock_system_auth_id()
    if not mock_sys:
        sys.stderr.write("ERROR: no exchange_key=mock in GET /system-exchange-auths. Cannot create mock auth.\n")
        return 2
    title = spec.get("title") or f"Mock-{spec.get('name', 'pm')}"
    c, d = api("POST", "/user-exchange-auths",
               body={"system_exchange_auth_id": mock_sys, "title": title, "credentials": {}})
    if c not in (200, 201) or not isinstance(d, dict):
        sys.stderr.write(f"ERROR: create mock user auth failed HTTP {c}: {d}\n")
        return 1
    mock_auth_id = d.get("id")
    print(f"mock user auth (paper trading): {mock_auth_id}  (title={title})")

    payload = {
        "name": spec["name"],
        "description": spec.get("description", f"{GOAL_LABELS.get(goal, goal)}+{STYLE_LABELS.get(style, style)} | mock {mock_auth_id}"),
        "exchange": spec.get("exchange", "mock"),
        "user_exchange_auth_id": mock_auth_id,
        "symbols": spec.get("symbols", []),
        "interval_ms": spec.get("interval_ms", 900000),  # always explicit — defaults are 1h for some goals
        "system_prompt": sys_prompt,
        "risk_config": risk,
        "llm_provider_id": spec.get("llm_provider_id", pid),
        "llm_model": spec.get("llm_model", model),
        "is_public": spec.get("is_public", True),
        "auto_start_scheduler": spec.get("auto_start_scheduler", True),
    }
    if spec.get("research_agent_ids"):
        payload["research_agent_ids"] = spec["research_agent_ids"]

    c, d = api("POST", "/agents/pm", body=payload)
    if c == 201 and isinstance(d, dict):
        agent_id = d.get("id") or d.get("agent_id")
        print(f"PM created: {agent_id}  name={spec['name']}  auto_start={payload['auto_start_scheduler']}")
        out = {"agent_id": agent_id, "mock_auth_id": mock_auth_id, "name": spec["name"],
               "goal": goal, "style": style, "interval_ms": payload["interval_ms"],
               "is_public": payload["is_public"], "response": d}
        if args.out:
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
            print(f"wrote -> {args.out}")
        if not payload["auto_start_scheduler"]:
            print("hint: auto_start was false — run `pm start --agent <id> --ms <interval>` to begin scheduling")
        else:
            print("hint: verify it actually runs with `pm status --agent <id>` (look for isSchedulerRunning + intervalMs)")
        return 0
    sys.stderr.write(f"ERROR: create PM failed HTTP {c}: {d}\n")
    return 1


def cmd_pm_start(args):
    c, d = api("POST", f"/agents/pm/{args.agent}/scheduler/start", body={"interval_ms": args.ms})
    print(f"scheduler/start HTTP {c}")
    if isinstance(d, dict):
        print(pretty(d))
    return 0 if c == 200 else 1


def cmd_pm_stop(args):
    c, d = api("POST", f"/agents/pm/{args.agent}/scheduler/stop", body={})
    print(f"scheduler/stop HTTP {c}")
    return 0 if c in (200, 204) else 1


def cmd_pm_status(args):
    c, d = api("GET", f"/agents/pm/{args.agent}/status")
    if args.raw or not isinstance(d, dict):
        print(pretty(d) if d else f"HTTP {c}")
    else:
        cur = d.get("currentRound") or {}
        rp = d.get("researchProgress") or {}
        print(f"agent            : {d.get('agentId')}")
        print(f"isSchedulerRunning: {d.get('isSchedulerRunning')}")
        print(f"intervalMs        : {d.get('intervalMs')}  (900000=15m, 1800000=30m, 3600000=1h)")
        print(f"phase             : {cur.get('phase')}  step={d.get('currentStep')}")
        print(f"nextRoundAt       : {cur.get('nextRoundAt') or d.get('nextRoundAt')}")
        if rp:
            done = sum(1 for v in rp.values() if isinstance(v, dict) and v.get("status") == "completed")
            print(f"researchProgress  : {done}/{len(rp)} completed")
    if c == 404:
        sys.stderr.write("  (404 here but the PM may still appear in `pm list` — known list/detail cache mismatch)\n")
    return 0 if c == 200 else 1


def cmd_pm_list(args):
    if args.limit < 1 or args.limit > 100:
        sys.stderr.write("--limit must be 1..100\n")
        return 2
    c, d = api("GET", f"/agents/pm?limit={args.limit}")
    if c == 200 and isinstance(d, dict):
        items = d.get("items") or d.get("data") or []
        total = d.get("total", len(items))
        print(f"total={total}  returned={len(items)}  base={base_url()}")
        for it in items:
            pub = "pub" if it.get("is_public") else "priv"
            tm = it.get("type_metadata") or {}
            print(f"  {it.get('id')}  [{pub}]  {str(it.get('name')):32}  interval={tm.get('interval_ms')}")
        print("\ntip: pick an id, then `pm status --agent <id>` / `logs pnl-summary --agent <id>`")
    else:
        print(pretty(d) if d else f"HTTP {c}")
    return 0 if c == 200 else 1


def cmd_pm_set_interval(args):
    print("1) stop scheduler")
    api("POST", f"/agents/pm/{args.agent}/scheduler/stop", body={})
    print("2) PUT interval_ms (CF Worker does NOT auto-sync — must restart)")
    c, d = api("PUT", f"/agents/pm/{args.agent}", body={"interval_ms": args.ms})
    print(f"   PUT HTTP {c}")
    print("3) start scheduler with explicit interval_ms")
    c2, _ = api("POST", f"/agents/pm/{args.agent}/scheduler/start", body={"interval_ms": args.ms})
    print(f"   start HTTP {c2}")
    return 0 if c in (200, 201) and c2 == 200 else 1


def cmd_pm_update(args):
    """Generalized stop→PUT→start for system_prompt / risk_config / symbols / name.
    CF Worker does NOT reload these from DB without a scheduler restart (坑①)."""
    if not args.field:
        sys.stderr.write("ERROR: --field required, one of: system_prompt risk_config symbols name\n")
        return 2
    body = {}
    for f in args.field:
        if f == "system_prompt":
            if not args.value:
                sys.stderr.write("system_prompt needs --value (or use --goal to rebuild from template)\n")
                return 2
            body["system_prompt"] = args.value
        elif f == "risk_config":
            if args.style:
                body["risk_config"] = RISK_TIERS[args.style]
            elif args.value:
                body["risk_config"] = json.loads(args.value)
            else:
                sys.stderr.write("risk_config needs --style <tier> or --value '<json>'\n")
                return 2
        elif f == "symbols":
            body["symbols"] = [s.strip().upper() for s in (args.value or "").split(",") if s.strip()]
        elif f == "name":
            body["name"] = args.value
        else:
            sys.stderr.write(f"unknown field: {f}\n")
            return 2
    print(f"update fields={list(body)} via stop→PUT→start (CF Worker needs restart to reload)")
    api("POST", f"/agents/pm/{args.agent}/scheduler/stop", body={})
    c, d = api("PUT", f"/agents/pm/{args.agent}", body=body)
    print(f"  PUT HTTP {c}")
    # restart keeping current interval
    sc, sd = api("GET", f"/agents/pm/{args.agent}/status", quiet=True)
    ms = 900000
    if isinstance(sd, dict):
        ms = sd.get("intervalMs") or 900000
    c2, _ = api("POST", f"/agents/pm/{args.agent}/scheduler/start", body={"interval_ms": ms})
    print(f"  start HTTP {c2}  (interval_ms={ms})")
    return 0 if c in (200, 201) and c2 == 200 else 1


def cmd_pm_delete(args):
    print("1) stop scheduler (delete does NOT stop the CF Worker scheduler)")
    api("POST", f"/agents/pm/{args.agent}/scheduler/stop", body={})
    print("2) delete PM  (DELETE /agents/{id} — NOT /agents/pm/{id}, that 405s)")
    c, d = api("DELETE", f"/agents/{args.agent}")
    print(f"   DELETE HTTP {c}")
    if args.mock_auth:
        print(f"3) delete mock auth {args.mock_auth}")
        c2, _ = api("DELETE", f"/user-exchange-auths/{args.mock_auth}")
        print(f"   DELETE mock HTTP {c2}")
    else:
        print("   (skipped mock cleanup — pass --mock-auth <id> to also remove the paper-trading credential)")
    return 0 if c in (200, 204) else 1


# =========================================================================
# logs / observe
# =========================================================================
def _print_pnl_summary(d):
    # Be defensive about shape — varies by deployment.
    rows = []
    if isinstance(d, list):
        rows = d
    elif isinstance(d, dict):
        rows = d.get("items") or d.get("data") or d.get("summary") or d.get("results") or []
        if not rows and d.get("total_pnl") is not None:
            print(f"total_realized_pnl = {d.get('total_pnl')}")
    total = 0.0
    print(f"{'date/bucket':24} {'realized_pnl':>14} {'trades':>8}")
    for r in rows:
        if not isinstance(r, dict):
            continue
        bucket = r.get("bucket") or r.get("date") or r.get("day") or r.get("period") or "?"
        pnl = r.get("realized_pnl", r.get("pnl", r.get("total_pnl", 0)))
        n = r.get("trade_count", r.get("order_count", r.get("count", "")))
        try:
            total += float(pnl or 0)
        except Exception:
            pass
        print(f"{str(bucket):24} {pnl!s:>14} {n!s:>8}")
    if rows:
        print(f"{'TOTAL':24} {round(total,2):>14}")


def cmd_logs_pnl_summary(args):
    c, d = api("GET", f"/marketplace/pm-agents/{args.agent}/pnl/summary?page_size={args.page_size}",
               prefix=API_PREFIX)
    if c == 404:
        sys.stderr.write("  (404 — known list/detail cache mismatch; the PM may still be in `pm list`)\n")
    if args.raw or not isinstance(d, (list, dict)):
        print(pretty(d) if d else f"HTTP {c}")
    else:
        _print_pnl_summary(d)
    return 0 if c == 200 else 1


def cmd_logs_pnl_orders(args):
    c, d = api("GET", f"/agent-logs/pnl/orders?pm_id={args.agent}&page_size={args.page_size}",
               prefix=API_PREFIX)
    if c == 404:
        sys.stderr.write("  (404 — known list/detail cache mismatch)\n")
    if args.raw or not isinstance(d, (list, dict)):
        print(pretty(d) if d else f"HTTP {c}")
    else:
        rows = d if isinstance(d, list) else (d.get("items") or d.get("data") or d.get("orders") or [])
        total = 0.0
        print(f"{'time':20} {'action':14} {'side':6} {'size':>12} {'price':>10} {'pnl':>10}")
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = r.get("time") or r.get("timestamp") or r.get("created_at") or "?"
            act = r.get("action") or r.get("action_type") or "?"
            side = r.get("side") or "?"
            size = r.get("size") or r.get("amount") or ""
            price = r.get("price") or r.get("entry_price") or ""
            pnl = r.get("realized_pnl", r.get("pnl", 0))
            try:
                total += float(pnl or 0)
            except Exception:
                pass
            print(f"{str(t)[:20]:20} {str(act):14} {str(side):6} {str(size):>12} {str(price):>10} {pnl!s:>10}")
        if rows:
            print(f"{'TOTAL':20} {'':14} {'':6} {'':>12} {'':>10} {round(total,2):>10}")
        if not rows:
            print("(no rows parsed — try --raw to see the actual response shape)")
    return 0 if c == 200 else 1


def cmd_logs_positions(args):
    c, d = api("GET", f"/agent-logs/pm/{args.agent}/positions?status={args.status}&page_size={args.page_size}",
               prefix=API_PREFIX)
    if c == 404:
        sys.stderr.write("  (404 — known list/detail cache mismatch)\n")
    if args.raw or not isinstance(d, (list, dict)):
        print(pretty(d) if d else f"HTTP {c}")
    else:
        # positions live under events[].attrs (NOT items[])
        rows = d if isinstance(d, list) else (d.get("events") or d.get("items") or d.get("data") or [])
        attrs_list = []
        for r in rows:
            if isinstance(r, dict):
                attrs_list.append(r.get("attrs") if isinstance(r.get("attrs"), dict) else r)
        print(f"open positions: {len(attrs_list)}   (status={args.status})")
        print(f"{'side':6} {'size':>12} {'entryPrice':>12} {'leverage':>10} {'stopLoss':>10}")
        for a in attrs_list:
            print(f"{str(a.get('side','?')):6} {str(a.get('size','')):>12} "
                  f"{str(a.get('entryPrice', a.get('entry_price',''))):>12} "
                  f"{str(a.get('leverage','')):>10} {str(a.get('stopLoss', a.get('stop_loss',''))):>10}")
        if len(attrs_list) >= 6:
            sys.stderr.write(
                f"  ⚠ {len(attrs_list)} open positions — that is a lot. PMs should hold 1-3; "
                "more suggests the prompt's restraint is being ignored.\n"
            )
        if not attrs_list:
            print("(no rows parsed — try --raw to see the actual response shape)")
    return 0 if c == 200 else 1


# =========================================================================
# parser
# =========================================================================
def build_parser():
    p = argparse.ArgumentParser(
        prog="flowmax.py",
        description="Manage your agents on the Hubble Market Server (JWT). "
                    "JWT from FLOWMAX_JWT env; base from FLOWMAX_BASE (default staging).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # probe
    pr = sub.add_parser("probe", help="探测合法值 + mock id + 内嵌模板")
    pr.add_argument("--asset-type", default=None, help="展示该 asset_type 的 datasource 清单")
    pr.set_defaults(func=cmd_probe)

    # research
    rc = sub.add_parser("research", help="research agent 操作")
    rcs = rc.add_subparsers(dest="sub", required=True)

    rcc = rcs.add_parser("create", help="建 research agent（批量 --spec 或单个用 flags）")
    rcc.add_argument("--spec", default=None, help="JSON 数组/对象文件（批量）")
    rcc.add_argument("--name", default=None)
    rcc.add_argument("--description", default=None)
    rcc.add_argument("--prompt", default=None)
    rcc.add_argument("--asset-type", default="Crypto", dest="asset_type")
    rcc.add_argument("--analysis-type", default="Quantitative Analysis", dest="analysis_type")
    rcc.add_argument("--language", default="zh-CN")
    rcc.add_argument("--public", default=True, action="store_true")
    rcc.add_argument("--out", default=None)
    rcc.add_argument("--interval", type=float, default=4.0, help="每个创建间隔秒")
    rcc.add_argument("--wait", type=int, default=0, help="部署等待秒 (0=建完即返回)")
    rcc.add_argument("--poll", type=int, default=8)
    rcc.set_defaults(func=cmd_research_create)

    rcg = rcs.add_parser("get", help="research agent 详情（看 reconcile 挑中哪些数据源）")
    rcg.add_argument("--agent", required=True)
    rcg.set_defaults(func=cmd_research_get)

    rcj = rcs.add_parser("job", help="research 部署 job 进度")
    rcj.add_argument("--job", required=True)
    rcj.set_defaults(func=cmd_research_job)

    # pm
    pm = sub.add_parser("pm", help="PM agent 操作")
    pms = pm.add_subparsers(dest="sub", required=True)

    pmc = pms.add_parser("create", help="建 PM（自动建 mock 纸面凭证 + 组装 prompt/risk + 默认自启）")
    pmc.add_argument("--spec", default=None, help="JSON 文件（高级/批量）")
    pmc.add_argument("--name", default=None)
    pmc.add_argument("--symbol", default=None, help="逗号分隔，如 BTCUSDT 或 BTCUSDT,ETHUSDT")
    pmc.add_argument("--goal", default=None, choices=list(PROMPT_SEGMENTS),
                     help=f"策略目标 {' / '.join(f'{g}={GOAL_LABELS[g]}' for g in PROMPT_SEGMENTS)}")
    pmc.add_argument("--style", default="balanced", choices=list(RISK_TIERS),
                     help=f"风险档位 {' / '.join(f'{s}={STYLE_LABELS[s]}' for s in RISK_TIERS)}")
    pmc.add_argument("--interval", type=int, default=900000, help="决策周期 ms (默认 900000=15min)")
    pmc.add_argument("--research-ids", default=None, dest="research_ids", help="逗号分隔的 research agent id")
    pmc.add_argument("--exit-policy", default=None, dest="exit_policy", choices=list(EXIT_POLICIES))
    pmc.add_argument("--overlay", default=None, choices=list(OVERLAYS),
                     help="追加固件：hard-stop(推荐,真实交易) / multi-trade(会堆仓,慎用)")
    pmc.add_argument("--extra-prompt", default=None, dest="extra_prompt", help="追加任意 prompt 文本")
    pmc.add_argument("--public", default=True, action="store_true")
    pmc.add_argument("--no-auto-start", dest="auto_start", action="store_false", default=True,
                     help="建完不自动启 scheduler（批量/错峰时用）")
    pmc.add_argument("--out", default=None)
    pmc.set_defaults(func=cmd_pm_create)

    def _agent(p):
        p.add_argument("--agent", required=True)

    pstart = pms.add_parser("start", help="启 scheduler（必须显式 --ms）")
    _agent(pstart); pstart.add_argument("--ms", type=int, default=900000)
    pstart.set_defaults(func=cmd_pm_start)

    pstop = pms.add_parser("stop", help="停 scheduler")
    _agent(pstop); pstop.set_defaults(func=cmd_pm_stop)

    pstatus = pms.add_parser("status", help="PM 实时状态")
    _agent(pstatus); pstatus.add_argument("--raw", action="store_true")
    pstatus.set_defaults(func=cmd_pm_status)

    plist = pms.add_parser("list", help="列 PM（用 limit, max 100）")
    plist.add_argument("--limit", type=int, default=50)
    plist.set_defaults(func=cmd_pm_list)

    pinterval = pms.add_parser("set-interval", help="改 interval（stop+PUT+start）")
    _agent(pinterval); pinterval.add_argument("--ms", type=int, default=900000)
    pinterval.set_defaults(func=cmd_pm_set_interval)

    pupdate = pms.add_parser("update", help="改 prompt/risk/symbols/name（stop+PUT+start，坑①）")
    _agent(pupdate)
    pupdate.add_argument("--field", action="append", required=True,
                         choices=["system_prompt", "risk_config", "symbols", "name"])
    pupdate.add_argument("--value", default=None, help="新值（risk_config 可改用 --style）")
    pupdate.add_argument("--style", default=None, choices=list(RISK_TIERS))
    pupdate.set_defaults(func=cmd_pm_update)

    pdel = pms.add_parser("delete", help="删 PM（stop+DELETE；可选删 mock）")
    _agent(pdel); pdel.add_argument("--mock-auth", default=None, dest="mock_auth")
    pdel.set_defaults(func=cmd_pm_delete)

    # logs
    lg = sub.add_parser("logs", help="查交易日志 / agent 信息")
    lgs = lg.add_subparsers(dest="sub", required=True)

    def _agent_page(p):
        p.add_argument("--agent", required=True)
        p.add_argument("--page-size", type=int, default=200, dest="page_size")
        p.add_argument("--raw", action="store_true", help="直接打印原始 JSON（字段口径变化时用）")

    lps = lgs.add_parser("pnl-summary", help="每日已实现 PnL（看谁亏）")
    _agent_page(lps); lps.set_defaults(func=cmd_logs_pnl_summary)

    lpo = lgs.add_parser("pnl-orders", help="逐单盈亏（看单笔爆仓/胜亏比）")
    _agent_page(lpo); lpo.set_defaults(func=cmd_logs_pnl_orders)

    lpos = lgs.add_parser("positions", help="当前持仓（看是否堆仓；字段在 events[].attrs）")
    _agent_page(lpos); lpos.add_argument("--status", default="open", choices=["open", "closed"])
    lpos.set_defaults(func=cmd_logs_positions)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
