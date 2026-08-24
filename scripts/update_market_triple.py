# -*- coding: utf-8 -*-
"""
Daily triple market dashboard:
  1) SDLLMTK price (Silicon Data public embed, ~7d merge)
  2) OpenRouter token usage (MacroMicro chart 148532, billion tokens / day)
  3) Expenditure proxy = price(USD/Mtok) * usage(billion tok) * 1000
     -> approximate USD/day spend on that usage surface
     (user label: 价格*用量 as market size proxy; NOT percentage share)

MacroMicro /charts/data needs headed Chromium (Cloudflare). headless often 403.

Usage:
  python update_market_triple.py
  python update_market_triple.py --skip-mm   # only price + recompute product
  python update_market_triple.py --import-mm path/to/chart_data_raw.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DIR = Path(__file__).resolve().parent
BACKUP = DIR / "Backup"
PRICE_CSV = DIR / "sdllmtk_daily.csv"
USAGE_CSV = DIR / "openrouter_usage_daily.csv"
PRODUCT_CSV = DIR / "expenditure_proxy_daily.csv"
META = DIR / "market_triple_meta.json"
CHART_PRICE = DIR / "sdllmtk_chart.html"
CHART_USAGE = DIR / "openrouter_usage_chart.html"
CHART_PRODUCT = DIR / "expenditure_proxy_chart.html"
CHART_DASHBOARD = DIR / "market_triple_dashboard.html"
USAGE_WEEKLY_CSV = DIR / "openrouter_usage_weekly.csv"
PRODUCT_WEEKLY_CSV = DIR / "expenditure_proxy_weekly.csv"

# MacroMicro-like palette (stack order bottom -> top; Total is line on top)
PROVIDER_STYLE = [
    ("DeepSeek", "#6b4f3a", True),
    ("Zhipu", "#e879a8", True),
    ("Qwen", "#9b8ec4", True),
    ("Minimax", "#5ec8d6", True),
    ("Moonshot", "#e85d4c", True),
    ("Mistral", "#2c5f8a", True),
    ("Arcee", "#9b6bdb", True),
    ("xAI", "#3d3d45", True),
    ("OpenAI", "#5cb85c", True),
    ("Anthropic", "#f0b429", True),
    ("Google", "#3bafda", True),
    ("Total", "#111111", False),  # line only, not stacked fill
]

EMBED_URL = "https://portal.silicondata.com/token-index-chart"
LATEST_DATE_URL = "https://api.silicondata.com/backend/api/token-index/latest-date"
MM_PAGE = "https://www.macromicro.me/charts/148532/world-openrouter-token-usage"
MM_DATA = "https://www.macromicro.me/charts/data/148532"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# 1 billion tokens = 1000 million tokens; SDLLMTK = USD per million tokens
BILLION_TO_MILLION = 1000.0


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path: Path) -> None:
    if not path.exists():
        return
    BACKUP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP / f"{path.stem}-{_now_ts()}{path.suffix}")


def http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------- price ----------
def fetch_sdllmtk_window() -> dict[str, float]:
    html = http_get(EMBED_URL)
    m = re.search(r'indexes\\?"\s*:\s*(\{(?:\\.|[^\\}])*?\})', html)
    if not m:
        m = re.search(r'"indexes"\s*:\s*(\{[^}]+\})', html)
    if not m:
        raise RuntimeError("SDLLMTK indexes not found")
    block = m.group(1)
    if '\\"' in block:
        block = block.replace('\\"', '"')
    try:
        indexes = json.loads(block)
    except json.JSONDecodeError:
        pairs = re.findall(r"(20\d{2}-\d{2}-\d{2})\\?\"\s*:\s*\\?\"([0-9.]+)\\?\"", m.group(0))
        indexes = {d: v for d, v in pairs}
    out = {}
    for d, v in indexes.items():
        out[d] = float(v)
    if not out:
        raise RuntimeError("empty SDLLMTK window")
    return out


def load_price_rows() -> list[dict]:
    if not PRICE_CSV.exists():
        return []
    rows = []
    with PRICE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "date": r["date"].strip(),
                    "value": float(r["value"]),
                    "source": (r.get("source") or "").strip(),
                    "note": (r.get("note") or "").strip(),
                }
            )
    rows.sort(key=lambda x: x["date"])
    return rows


def merge_price(existing: list[dict], window: dict[str, float]) -> list[dict]:
    rank = {
        "public_embed": 100,
        "portal_api": 100,
        "press_cio": 50,
        "approx_chart": 10,
        "manual": 60,
    }
    by = {r["date"]: r for r in existing}
    for d, v in window.items():
        src = "public_embed"
        if d not in by or rank.get(src, 0) >= rank.get(by[d].get("source"), 0):
            by[d] = {"date": d, "value": v, "source": src, "note": "portal token-index-chart RSC"}
    return [by[k] for k in sorted(by)]


def save_price(rows: list[dict]) -> None:
    with PRICE_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "value", "source", "note"])
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "date": r["date"],
                    "value": f"{r['value']:.4f}",
                    "source": r.get("source", ""),
                    "note": r.get("note", ""),
                }
            )


def price_series_daily(rows: list[dict]) -> dict[str, float]:
    """Anchor list -> daily linear interpolation."""
    if not rows:
        return {}
    rows = sorted(rows, key=lambda x: x["date"])
    out: dict[str, float] = {}
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        da = datetime.strptime(a["date"], "%Y-%m-%d").date()
        db = datetime.strptime(b["date"], "%Y-%m-%d").date()
        n = (db - da).days
        out[a["date"]] = a["value"]
        if n <= 1:
            continue
        for k in range(1, n):
            t = k / n
            d = (da + timedelta(days=k)).isoformat()
            out[d] = a["value"] * (1 - t) + b["value"] * t
    out[rows[-1]["date"]] = rows[-1]["value"]
    return out


# ---------- usage MacroMicro ----------
def parse_mm_payload(payload: dict) -> dict[str, dict[str, float]]:
    """Return {provider: {date: value_b}} including Total sum."""
    block = payload["data"]["c:148532"]
    cfgs = block["info"]["chart_config"]["seriesConfigs"]
    series = block["series"]
    by_prov: dict[str, dict[str, float]] = {}
    for i, cfg in enumerate(cfgs):
        name = (cfg.get("name_en") or cfg.get("name_tc") or f"s{i}").strip()
        # normalize
        key = name.replace(" ", "")
        if key.lower() == "openai":
            key = "OpenAI"
        elif key.lower() == "x-ai":
            key = "xAI"
        elif key.lower() == "deepseek":
            key = "DeepSeek"
        elif key.lower() == "moonshotai":
            key = "Moonshot"
        elif key.lower() == "arcee-ai":
            key = "Arcee"
        elif key.lower() == "z-ai":
            key = "Zhipu"
        m: dict[str, float] = {}
        for pt in series[i]:
            d, v = pt[0], float(pt[1])
            m[d] = v
        by_prov[key] = m
    # Total
    all_dates = sorted({d for m in by_prov.values() for d in m})
    total = {}
    for d in all_dates:
        total[d] = sum(m.get(d, 0.0) for m in by_prov.values())
    by_prov["Total"] = total
    return by_prov


def fetch_mm_via_playwright() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-TW",
            user_agent=UA,
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        captured = {"raw": None}

        def on_response(resp):
            if "charts/data/148532" in resp.url and resp.status == 200:
                try:
                    t = resp.text()
                    if len(t) > 500:
                        captured["raw"] = t
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(MM_PAGE, wait_until="domcontentloaded", timeout=120000)
        for _ in range(40):
            page.wait_for_timeout(1500)
            if captured["raw"]:
                break
            try:
                r = page.evaluate(
                    """async () => {
                      const r = await fetch('/charts/data/148532', {credentials:'include'});
                      return {status: r.status, body: await r.text()};
                    }"""
                )
                if r.get("status") == 200 and len(r.get("body") or "") > 500:
                    captured["raw"] = r["body"]
                    break
            except Exception:
                pass
        browser.close()
        if not captured["raw"]:
            raise RuntimeError("MacroMicro chart data not captured (CF/login?)")
        return json.loads(captured["raw"])


def usage_to_rows(by_prov: dict[str, dict[str, float]]) -> list[dict]:
    providers = [p for p in by_prov if p != "Total"]
    providers_sorted = sorted(providers)
    dates = sorted(by_prov["Total"].keys())
    rows = []
    for d in dates:
        row = {"date": d, "Total": by_prov["Total"][d]}
        for p in providers_sorted:
            row[p] = by_prov[p].get(d, "")
        rows.append(row)
    return rows, providers_sorted


def save_usage(rows: list[dict], providers: list[str]) -> None:
    fields = ["date", "Total"] + providers
    with USAGE_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {"date": r["date"], "Total": f"{float(r['Total']):.4f}"}
            for p in providers:
                v = r.get(p, "")
                out[p] = f"{float(v):.4f}" if v != "" else ""
            w.writerow(out)


def load_usage_total() -> dict[str, float]:
    if not USAGE_CSV.exists():
        return {}
    out = {}
    with USAGE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            out[r["date"]] = float(r["Total"])
    return out


# ---------- product ----------
def build_product(price_daily: dict[str, float], usage_total: dict[str, float]) -> list[dict]:
    dates = sorted(set(price_daily) & set(usage_total))
    rows = []
    for d in dates:
        p = price_daily[d]
        u = usage_total[d]
        # USD per day proxy
        spend = p * u * BILLION_TO_MILLION
        rows.append(
            {
                "date": d,
                "price_sdllmtk": p,
                "usage_total_b": u,
                "spend_usd_day": spend,
                "product_raw": p * u,  # price * billion-tokens (compact index)
            }
        )
    return rows


def save_product(rows: list[dict]) -> None:
    with PRODUCT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["date", "price_sdllmtk", "usage_total_b", "spend_usd_day", "product_raw"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "date": r["date"],
                    "price_sdllmtk": f"{r['price_sdllmtk']:.4f}",
                    "usage_total_b": f"{r['usage_total_b']:.4f}",
                    "spend_usd_day": f"{r['spend_usd_day']:.2f}",
                    "product_raw": f"{r['product_raw']:.4f}",
                }
            )


def aggregate_product_weekly(
    price_by_date: dict[str, float],
    usage_by_date: dict[str, float],
) -> list[dict]:
    """Week spend = avg(available SDLLMTK in week) × sum(usage week) × 1000.

    Usage must be a complete ISO week (7 days). Price may lag 1 day behind usage;
    use whatever price points exist in that week (need ≥1). Do NOT require
    price∩usage overlap to also be 7 days — that wrongly drops the newest week.
    """
    buckets: dict[tuple[int, int], dict] = {}
    for ds, u in usage_by_date.items():
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        y, w, end = _iso_week_key(d)
        key = (y, w)
        if key not in buckets:
            buckets[key] = {
                "date": end,
                "week": f"{y}-W{w:02d}",
                "usage_days": 0,
                "usage_sum": 0.0,
                "price_sum": 0.0,
                "price_days": 0,
            }
        b = buckets[key]
        b["usage_days"] += 1
        b["usage_sum"] += float(u)
        if ds in price_by_date:
            b["price_sum"] += float(price_by_date[ds])
            b["price_days"] += 1
    out: list[dict] = []
    for key in sorted(buckets):
        b = buckets[key]
        if b["usage_days"] < 7 or b["price_days"] < 1:
            continue
        avg_p = b["price_sum"] / b["price_days"]
        spend = avg_p * b["usage_sum"] * BILLION_TO_MILLION
        out.append(
            {
                "date": b["date"],
                "week": b["week"],
                "days": b["usage_days"],
                "price_days": b["price_days"],
                "price_sdllmtk_avg": avg_p,
                "usage_total_b_sum": b["usage_sum"],
                "spend_usd_week": spend,
            }
        )
    return out


# ---------- charts ----------
def _chart_shell(title: str, subtitle: str, body_js_data: str, y_label: str, color: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#0b0b0c;color:#e8e8ea}}
 .wrap{{max-width:1120px;margin:0 auto;padding:24px}}
 h1{{font-size:20px;margin:0 0 6px}}
 .sub{{color:#9a9aa0;font-size:13px;margin-bottom:14px;line-height:1.45}}
 .card{{background:#141416;border:1px solid #2a2a2e;border-radius:12px;padding:16px}}
 .meta{{display:flex;flex-wrap:wrap;gap:20px;margin-bottom:10px;font-size:13px;color:#b0b0b6}}
 .meta b{{color:#fff;font-size:22px;display:block;margin-top:2px}}
 canvas{{width:100%!important;height:440px!important}}
 .legend{{font-size:12px;color:#8a8a90;margin-top:12px;line-height:1.55}}
 a{{color:#7ec8e3}}
</style>
</head>
<body>
<div class="wrap">
 <h1>{title}</h1>
 <div class="sub">{subtitle}</div>
 <div class="card">
  <div class="meta" id="meta"></div>
  <canvas id="c"></canvas>
  <div class="legend" id="leg"></div>
 </div>
</div>
<script>
const rows = {body_js_data};
const labels = rows.map(r => r.date);
const values = rows.map(r => r.value);
const last = rows[rows.length-1], first = rows[0];
document.getElementById('meta').innerHTML =
  `<div>Latest<br><b>${{last?last.value.toLocaleString(undefined,{{maximumFractionDigits:4}}):'—'}}</b>${{last?last.date:''}}</div>`+
  `<div>Range<br><b>${{first?first.date:''}}</b>→ ${{last?last.date:''}} · n=${{rows.length}}</div>`;
Chart.register({{
  id:'crosshair',
  afterDatasetsDraw(chart) {{
    const a = chart.tooltip?.getActiveElements?.() || [];
    if (!a.length) return;
    const x = a[0].element.x, {{top,bottom}}=chart.chartArea, ctx=chart.ctx;
    ctx.save(); ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,bottom);
    ctx.setLineDash([4,3]); ctx.strokeStyle='rgba(255,255,255,.45)'; ctx.stroke(); ctx.restore();
  }}
}});
new Chart(document.getElementById('c'), {{
  type:'line',
  data:{{labels, datasets:[{{
    label:'{y_label}',
    data:values, borderColor:'{color}', backgroundColor:'{color}22',
    fill:true, tension:0.2, borderWidth:2, pointRadius:0, pointHoverRadius:5, pointHitRadius:16
  }}]}},
  options:{{
    responsive:true, maintainAspectRatio:false,
    interaction:{{mode:'index', intersect:false}},
    hover:{{mode:'index', intersect:false}},
    plugins:{{
      legend:{{display:false}},
      tooltip:{{
        enabled:true, mode:'index', intersect:false,
        backgroundColor:'rgba(20,20,24,.94)', padding:10,
        callbacks:{{ label:(c)=>` ${{Number(c.parsed.y).toLocaleString(undefined,{{maximumFractionDigits:4}})}}` }}
      }}
    }},
    scales:{{
      x:{{ticks:{{color:'#888', maxTicksLimit:14, maxRotation:0}}, grid:{{color:'#ffffff10'}}}},
      y:{{ticks:{{color:'#888'}}, grid:{{color:'#ffffff14'}},
          title:{{display:true, text:'{y_label}', color:'#aaa'}}}}
    }}
  }}
}});
</script>
</div>
</body></html>
"""


def write_price_chart(price_daily: dict[str, float], price_rows: list[dict]) -> None:
    series = [{"date": d, "value": round(v, 4)} for d, v in sorted(price_daily.items())]
    off = [r for r in price_rows if r.get("source") == "public_embed"]
    html = _chart_shell(
        "① SDLLMTK 價格（USD / 百萬 tokens）",
        "Silicon Data LLM Token Expenditure Index · 官方近7日 + 歷史錨點插值 · "
        f"<a href='file:///{CHART_USAGE.as_posix()}'>用量圖</a> · "
        f"<a href='file:///{CHART_PRODUCT.as_posix()}'>支出規模圖</a>",
        json.dumps(series, ensure_ascii=False),
        "USD per million tokens",
        "#f5a623",
    )
    # inject official note
    html = html.replace(
        'id="leg"></div>',
        f'id="leg">官方 public_embed 點數：{len(off)} · 其餘為圖表錨點/插值，非完整官方日收。</div>',
    )
    CHART_PRICE.write_text(html, encoding="utf-8")


def _iso_week_key(d: date) -> tuple[int, int, str]:
    """Return (iso_year, iso_week, label week_ending_sunday YYYY-MM-DD)."""
    y, w, _ = d.isocalendar()
    # Monday=1 .. Sunday=7
    week_end = d + timedelta(days=(7 - d.isoweekday()))
    return y, w, week_end.isoformat()


def aggregate_usage_weekly(usage_rows: list[dict], providers: list[str]) -> list[dict]:
    """Sum daily billion-tokens into complete ISO weeks only (Mon-Sun, days==7).

    Label = week end (Sunday). Incomplete weeks (e.g. current week mid-week) are
    excluded so week-mode bars are comparable; next bar appears after that Sunday.
    """
    buckets: dict[tuple[int, int], dict] = {}
    order: list[tuple[int, int]] = []
    for r in usage_rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        y, w, end = _iso_week_key(d)
        key = (y, w)
        if key not in buckets:
            buckets[key] = {
                "date": end,
                "week": f"{y}-W{w:02d}",
                "days": 0,
                "Total": 0.0,
                **{p: 0.0 for p in providers},
            }
            order.append(key)
        b = buckets[key]
        b["days"] += 1
        b["Total"] += float(r.get("Total") or 0)
        for p in providers:
            v = r.get(p, "")
            if v != "" and v is not None:
                b[p] += float(v)
    # only full weeks — partial week looks like a crash on the chart
    return [buckets[k] for k in sorted(order) if buckets[k]["days"] >= 7]


def save_usage_weekly(rows: list[dict], providers: list[str]) -> None:
    fields = ["date", "week", "days", "Total"] + providers
    with USAGE_WEEKLY_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {
                "date": r["date"],
                "week": r["week"],
                "days": r["days"],
                "Total": f"{float(r['Total']):.4f}",
            }
            for p in providers:
                out[p] = f"{float(r.get(p) or 0):.4f}"
            w.writerow(out)


def write_usage_chart(usage_rows: list[dict]) -> None:
    """Multi-brand stacked chart + legend click toggle + day/week switch."""
    if not usage_rows:
        return
    # discover providers from first row
    sample = usage_rows[0]
    raw_cols = [k for k in sample.keys() if k not in ("date", "Total", "week", "days")]
    # keep style order then any extras
    style_names = [n for n, _, _ in PROVIDER_STYLE if n != "Total"]
    providers = [n for n in style_names if n in raw_cols]
    for c in sorted(raw_cols):
        if c not in providers:
            providers.append(c)

    weekly = aggregate_usage_weekly(usage_rows, providers)
    save_usage_weekly(weekly, providers)

    # payload for JS
    def pack(rows: list[dict], date_key: str = "date") -> dict:
        labels = [r[date_key] for r in rows]
        series = {}
        for name in providers + ["Total"]:
            series[name] = [round(float(r.get(name) or 0), 4) for r in rows]
        return {"labels": labels, "series": series}

    payload = {
        "day": pack(usage_rows),
        "week": pack(weekly),
        "providers": providers,
        "styles": {n: {"color": c, "stack": s} for n, c, s in PROVIDER_STYLE},
        "defaultMode": "week",
    }
    # ensure all providers have style
    for p in providers:
        if p not in payload["styles"]:
            payload["styles"][p] = {"color": "#888888", "stack": True}
    if "Total" not in payload["styles"]:
        payload["styles"]["Total"] = {"color": "#111111", "stack": False}

    data_json = json.dumps(payload, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>② OpenRouter Token 使用量（多品牌 · 日/週）</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#f7f8fa;color:#1a1a1a}}
 .wrap{{max-width:1200px;margin:0 auto;padding:20px 24px 40px}}
 h1{{font-size:22px;margin:0 0 4px;font-weight:600}}
 .sub{{color:#666;font-size:13px;margin-bottom:14px;line-height:1.5}}
 .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 18px 12px;box-shadow:0 1px 2px #0001}}
 .toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}}
 .seg{{display:inline-flex;border:1px solid #d0d5dd;border-radius:8px;overflow:hidden}}
 .seg button{{border:0;background:#fff;padding:7px 14px;font-size:13px;cursor:pointer;color:#444}}
 .seg button.on{{background:#12b886;color:#fff;font-weight:600}}
 .act{{display:inline-flex;gap:6px;margin-left:4px}}
 .act button{{border:1px solid #d0d5dd;background:#fff;padding:6px 12px;font-size:12px;cursor:pointer;border-radius:8px;color:#333}}
 .act button:hover{{border-color:#12b886;color:#0a8}}
 .hint{{font-size:12px;color:#888;margin-left:4px}}
 .meta{{display:flex;flex-wrap:wrap;gap:18px;font-size:12px;color:#666;margin-bottom:8px}}
 .meta b{{color:#111;font-size:18px}}
 .chart-box{{position:relative;height:480px}}
 .foot{{font-size:12px;color:#888;margin-top:10px;line-height:1.55}}
 a{{color:#0b7;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>全球-OpenRouter Token 使用量</h1>
  <div class="sub">
    各品牌可點圖例 <b>顯示/取消</b> · 可用 <b>全部取消</b> 後再單點一品牌 · 預設 <b>週加總</b>（ISO 週一～日合計，標籤=該週日）·
    資料 OpenRouter Datasets API ·
    <a href="file:///{CHART_PRODUCT.as_posix()}">支出規模圖</a> ·
    <a href="file:///{CHART_PRICE.as_posix()}">價格圖</a>
  </div>
  <div class="card">
    <div class="toolbar">
      <div class="seg" id="modeSeg">
        <button type="button" data-mode="day">日</button>
        <button type="button" data-mode="week" class="on">週加總</button>
      </div>
      <div class="act">
        <button type="button" id="btnAllOn" title="顯示所有品牌 + Total">全部顯示</button>
        <button type="button" id="btnAllOff" title="取消所有品牌與 Total，再點圖例單選">全部取消</button>
      </div>
      <span class="hint">點下方圖例切換；「全部取消」後再點一個品牌即可單看。Total 為黑線（不進堆疊）。</span>
    </div>
    <div class="meta" id="meta"></div>
    <div class="chart-box"><canvas id="c"></canvas></div>
    <div class="foot">單位：billion tokens。週模式 = 完整 ISO 週（一～日共 7 日）合計；未滿週不畫（下一根要等該週日後）。來源：OpenRouter Datasets API</div>
  </div>
</div>
<script>
const PAYLOAD = {data_json};
const orderStack = PAYLOAD.providers.slice(); // bottom-> already style order
const names = orderStack.concat(['Total']);

function buildDatasets(mode) {{
  const block = PAYLOAD[mode];
  const styles = PAYLOAD.styles;
  const ds = [];
  // stacked brands first
  for (const name of orderStack) {{
    const st = styles[name] || {{color:'#888', stack:true}};
    ds.push({{
      label: name,
      data: block.series[name] || [],
      borderColor: st.color,
      backgroundColor: st.color,
      borderWidth: 0.5,
      pointRadius: 0,
      fill: true,
      tension: 0.05,
      stack: 'usage',
      order: 2,
      hidden: false,
    }});
  }}
  // Total line on top (non-stacked axis so hide brands does not warp the line)
  const stT = styles.Total || {{color:'#111'}};
  ds.push({{
    label: 'Total',
    data: block.series.Total || [],
    borderColor: stT.color,
    backgroundColor: 'transparent',
    borderWidth: 2.2,
    pointRadius: 0,
    fill: false,
    tension: 0.05,
    yAxisID: 'yTotal',
    order: 0,
    hidden: false,
  }});
  return {{ labels: block.labels, datasets: ds }};
}}

let mode = PAYLOAD.defaultMode || 'week';
let chart;
/** labels currently hidden — survive day/week switch */
const hiddenLabels = new Set();

function yTitle(m) {{
  return m === 'week' ? 'billion tokens / week (sum)' : 'billion tokens / day';
}}

function refreshMeta(m) {{
  const block = PAYLOAD[m];
  const labels = block.labels;
  const tot = block.series.Total || [];
  const last = tot[tot.length-1];
  const first = labels[0], end = labels[labels.length-1];
  document.getElementById('meta').innerHTML =
    `<div>Latest Total<br><b>${{last!=null ? last.toLocaleString(undefined,{{maximumFractionDigits:1}}) : '—'}}</b> ${{end}} · mode=${{m}}</div>` +
    `<div>Range<br><b>${{first}}</b> → ${{end}} · n=${{labels.length}}</div>`;
}}

function syncHiddenFromChart() {{
  if (!chart) return;
  hiddenLabels.clear();
  chart.data.datasets.forEach((ds, i) => {{
    if (!chart.isDatasetVisible(i)) hiddenLabels.add(ds.label);
  }});
}}

function applyHiddenToChart() {{
  if (!chart) return;
  chart.data.datasets.forEach((ds, i) => {{
    chart.setDatasetVisibility(i, !hiddenLabels.has(ds.label));
  }});
  chart.update();
  const yScale = chart.scales.y;
  if (yScale && chart.scales.yTotal) {{
    chart.scales.yTotal.options.max = yScale.max;
    chart.scales.yTotal.options.min = 0;
    chart.update('none');
  }}
}}

function setAllVisible(on) {{
  if (!chart) return;
  hiddenLabels.clear();
  if (!on) {{
    chart.data.datasets.forEach(ds => hiddenLabels.add(ds.label));
  }}
  applyHiddenToChart();
}}

if (!window.__xh) {{
  window.__xh = true;
  Chart.register({{
    id: 'crosshair',
    afterDatasetsDraw(chart) {{
      const a = chart.tooltip?.getActiveElements?.() || [];
      if (!a.length) return;
      const x = a[0].element.x, {{top, bottom}} = chart.chartArea, ctx = chart.ctx;
      ctx.save(); ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom);
      ctx.setLineDash([4, 3]); ctx.strokeStyle = 'rgba(0,0,0,0.35)'; ctx.stroke(); ctx.restore();
    }}
  }});
}}

function render(m) {{
  mode = m;
  const data = buildDatasets(m);
  refreshMeta(m);
  document.querySelectorAll('#modeSeg button').forEach(btn => {{
    btn.classList.toggle('on', btn.dataset.mode === m);
  }});
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('c'), {{
    type: 'line',
    data,
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      hover: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{
          display: true,
          position: 'bottom',
          labels: {{
            usePointStyle: true,
            pointStyle: 'circle',
            boxWidth: 10,
            padding: 14,
            color: '#333',
            font: {{ size: 12 }}
          }},
          onClick(e, legendItem, legend) {{
            const ch = legend.chart;
            const idx = legendItem.datasetIndex;
            ch.setDatasetVisibility(idx, !ch.isDatasetVisible(idx));
            ch.update();
            syncHiddenFromChart();
            const yScale = ch.scales.y;
            if (yScale && ch.scales.yTotal) {{
              ch.scales.yTotal.options.max = yScale.max;
              ch.scales.yTotal.options.min = 0;
              ch.update('none');
            }}
          }}
        }},
        tooltip: {{
          enabled: true,
          mode: 'index',
          intersect: false,
          backgroundColor: 'rgba(255,255,255,0.96)',
          titleColor: '#111',
          bodyColor: '#333',
          borderColor: '#ddd',
          borderWidth: 1,
          padding: 10,
          itemSort: (a, b) => (b.parsed.y || 0) - (a.parsed.y || 0),
          callbacks: {{
            title: (items) => items.length ? String(items[0].label) : '',
            label: (ctx) => ` ${{ctx.dataset.label}}: ${{Number(ctx.parsed.y).toLocaleString(undefined, {{maximumFractionDigits: 2}})}} b`,
            footer: (items) => {{
              let sum = 0;
              items.forEach(it => {{
                if (it.dataset.label !== 'Total') sum += Number(it.parsed.y)||0;
              }});
              return 'visible brands sum ' + sum.toFixed(1);
            }}
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ maxTicksLimit: 16, maxRotation: 0, color: '#666' }},
          grid: {{ color: '#eee' }}
        }},
        y: {{
          stacked: true,
          beginAtZero: true,
          title: {{ display: true, text: yTitle(m), color: '#666' }},
          ticks: {{ color: '#666' }},
          grid: {{ color: '#eee' }}
        }},
        yTotal: {{
          stacked: false,
          beginAtZero: true,
          display: false,
          grid: {{ drawOnChartArea: false }},
          min: 0
        }}
      }}
    }}
  }});
  applyHiddenToChart();
}}

document.getElementById('modeSeg').addEventListener('click', (e) => {{
  const btn = e.target.closest('button[data-mode]');
  if (!btn) return;
  render(btn.dataset.mode);
}});
document.getElementById('btnAllOn').addEventListener('click', () => setAllVisible(true));
document.getElementById('btnAllOff').addEventListener('click', () => setAllVisible(false));

render(mode);
</script>
</body>
</html>
"""
    CHART_USAGE.write_text(html, encoding="utf-8")


def write_product_chart(
    product_rows: list[dict],
    price_daily: dict[str, float] | None = None,
    usage_total: dict[str, float] | None = None,
) -> None:
    """Price x usage with day/week; week uses weekly usage sum * week-avg price."""
    if not product_rows:
        return
    # daily series already has spend_usd_day
    day_series = [
        {
            "date": r["date"],
            "value": round(r["spend_usd_day"], 2),
            "price": r["price_sdllmtk"],
            "usage_b": r["usage_total_b"],
        }
        for r in product_rows
    ]
    # weekly: complete usage week × avg of available prices in that week
    if price_daily is None:
        price_daily = {r["date"]: float(r["price_sdllmtk"]) for r in product_rows}
    if usage_total is None:
        usage_total = {r["date"]: float(r["usage_total_b"]) for r in product_rows}
    week_rows = aggregate_product_weekly(price_daily, usage_total)
    with PRODUCT_WEEKLY_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "week",
                "days",
                "price_sdllmtk_avg",
                "usage_total_b_sum",
                "spend_usd_week",
            ],
        )
        w.writeheader()
        for r in week_rows:
            w.writerow(
                {
                    "date": r["date"],
                    "week": r["week"],
                    "days": r["days"],
                    "price_sdllmtk_avg": f"{r['price_sdllmtk_avg']:.4f}",
                    "usage_total_b_sum": f"{r['usage_total_b_sum']:.4f}",
                    "spend_usd_week": f"{r['spend_usd_week']:.2f}",
                }
            )

    payload = {
        "day": {
            "labels": [r["date"] for r in day_series],
            "values": [r["value"] for r in day_series],
            "unit": "USD / day (proxy)",
        },
        "week": {
            "labels": [r["date"] for r in week_rows],
            "values": [round(r["spend_usd_week"], 2) for r in week_rows],
            "unit": "USD / week (proxy)",
        },
        "defaultMode": "week",
        "note": "week = avg(SDLLMTK) × sum(usage_b) × 1000",
    }
    data_json = json.dumps(payload, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>③ 市場支出規模 proxy（日/週）</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 body{{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:#0b0b0c;color:#e8e8ea}}
 .wrap{{max-width:1120px;margin:0 auto;padding:24px}}
 h1{{font-size:20px;margin:0 0 6px}}
 .sub{{color:#9a9aa0;font-size:13px;margin-bottom:14px;line-height:1.45}}
 .card{{background:#141416;border:1px solid #2a2a2e;border-radius:12px;padding:16px}}
 .seg{{display:inline-flex;border:1px solid #333;border-radius:8px;overflow:hidden;margin-bottom:12px}}
 .seg button{{border:0;background:#1a1a1c;padding:7px 14px;font-size:13px;cursor:pointer;color:#aaa}}
 .seg button.on{{background:#2f9e44;color:#fff;font-weight:600}}
 .meta{{display:flex;flex-wrap:wrap;gap:18px;font-size:12px;color:#aaa;margin-bottom:8px}}
 .meta b{{color:#fff;font-size:20px;display:block}}
 .chart-box{{height:440px}}
 .foot{{font-size:12px;color:#8a8a90;margin-top:10px;line-height:1.55}}
 a{{color:#7ec8e3}}
</style>
</head>
<body>
<div class="wrap">
  <h1>③ 市場支出規模 proxy（價格 × 使用量）</h1>
  <div class="sub">
    預設週加總（平滑週末）·
    <a href="file:///{CHART_USAGE.as_posix()}">用量多品牌圖</a> ·
    <a href="file:///{CHART_PRICE.as_posix()}">價格圖</a>
  </div>
  <div class="card">
    <div class="seg" id="modeSeg">
      <button type="button" data-mode="day">日</button>
      <button type="button" data-mode="week" class="on">週加總</button>
    </div>
    <div class="meta" id="meta"></div>
    <div class="chart-box"><canvas id="c"></canvas></div>
    <div class="foot">日：SDLLMTK×usage_b×1000。週：週均價×週用量合計×1000。非真實全球營收。</div>
  </div>
</div>
<script>
const P = {data_json};
let chart;
Chart.register({{
  id:'crosshair',
  afterDatasetsDraw(chart) {{
    const a = chart.tooltip?.getActiveElements?.() || [];
    if (!a.length) return;
    const x = a[0].element.x, {{top,bottom}}=chart.chartArea, ctx=chart.ctx;
    ctx.save(); ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,bottom);
    ctx.setLineDash([4,3]); ctx.strokeStyle='rgba(255,255,255,.45)'; ctx.stroke(); ctx.restore();
  }}
}});
function render(m) {{
  const block = P[m];
  document.querySelectorAll('#modeSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode===m));
  const last = block.values[block.values.length-1];
  document.getElementById('meta').innerHTML =
    `<div>Latest<br><b>${{last!=null?last.toLocaleString():'—'}}</b>${{block.labels[block.labels.length-1]}} · ${{m}}</div>`+
    `<div>n<br><b>${{block.labels.length}}</b>${{block.unit}}</div>`;
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('c'), {{
    type:'line',
    data:{{
      labels: block.labels,
      datasets:[{{
        label: 'Spend proxy',
        data: block.values,
        borderColor:'#7ee787',
        backgroundColor:'#7ee78722',
        fill:true, tension:0.2, borderWidth:2,
        pointRadius:0, pointHoverRadius:5, pointHitRadius:16
      }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      interaction:{{mode:'index', intersect:false}},
      hover:{{mode:'index', intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{
          enabled:true, mode:'index', intersect:false,
          backgroundColor:'rgba(20,20,24,.94)', padding:10,
          callbacks:{{
            title:(items)=> items.length?String(items[0].label):'',
            label:(c)=>` ${{Number(c.parsed.y).toLocaleString(undefined,{{maximumFractionDigits:0}})}} ${{block.unit}}`
          }}
        }}
      }},
      scales:{{
        x:{{ticks:{{color:'#888', maxTicksLimit:14, maxRotation:0}}, grid:{{color:'#ffffff10'}}}},
        y:{{ticks:{{color:'#888'}}, grid:{{color:'#ffffff14'}},
            title:{{display:true, text: block.unit, color:'#aaa'}}}}
      }}
    }}
  }});
}}
document.getElementById('modeSeg').addEventListener('click', e => {{
  const b = e.target.closest('button[data-mode]');
  if (b) render(b.dataset.mode);
}});
render(P.defaultMode||'week');
</script>
</body>
</html>
"""
    CHART_PRODUCT.write_text(html, encoding="utf-8")

def _parse_iso(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def _add_days(d: str, n: int) -> str:
    return (_parse_iso(d) + timedelta(days=n)).isoformat()


def _daily_labels(dmin: str, dmax: str) -> list[str]:
    """Inclusive daily labels from dmin to dmax (YYYY-MM-DD)."""
    a, b = _parse_iso(dmin), _parse_iso(dmax)
    out: list[str] = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _pad_daily_map(value_by_date: dict[str, float | None], dmin: str, dmax: str) -> tuple[list[str], list[float | None]]:
    labels = _daily_labels(dmin, dmax)
    values = [value_by_date.get(d) for d in labels]
    return labels, values


def _pad_weekly_map(
    value_by_date: dict[str, float | None], week_labels: list[str]
) -> tuple[list[str], list[float | None]]:
    """Align series to shared week_labels (Sunday ends); missing -> None."""
    return week_labels, [value_by_date.get(d) for d in week_labels]


def _shared_axis_from_usage(
    usage_rows: list[dict], weekly: list[dict]
) -> tuple[str, str, str, str, str, list[str]]:
    """Chart② master X domain.

    Returns (day_min, day_max_data, week_min, week_max_data, axis_max, week_labels).
    week_labels = complete weeks + one reserved empty Sunday (axis_max).
    """
    day_min = usage_rows[0]["date"]
    day_max_data = usage_rows[-1]["date"]
    if weekly:
        week_min = weekly[0]["date"]
        week_max_data = weekly[-1]["date"]
    else:
        _, _, week_min = _iso_week_key(_parse_iso(day_min))
        _, _, week_max_data = _iso_week_key(_parse_iso(day_max_data))
    axis_max = _add_days(week_max_data, 7)
    week_labels = [r["date"] for r in weekly] + [axis_max]
    return day_min, day_max_data, week_min, week_max_data, axis_max, week_labels



def write_combined_dashboard(
    price_daily: dict[str, float],
    price_rows: list[dict],
    usage_rows: list[dict],
    product_rows: list[dict],
) -> None:
    """One page: ① price ② multi-brand usage ③ expenditure proxy.

    Chart② owns the X domain. Right edge = last complete week end + 7 days
    (one reserved empty week). Charts ① and ③ pad/trim so head/tail match ②.
    """
    n_official = sum(1 for r in price_rows if r.get("source") == "public_embed")

    # raw price map (before axis pad)
    price_by_date: dict[str, float | None] = {
        d: round(v, 4) for d, v in sorted(price_daily.items())
    }
    last_p = None
    if price_by_date:
        ld = max(price_by_date)
        last_p = {"date": ld, "value": price_by_date[ld]}

    usage_payload = None
    product_payload = None
    last_u = None
    last_x = None
    axis_meta: dict | None = None

    day_min = day_max_data = week_min = week_max_data = axis_max = ""
    week_labels: list[str] = []

    if usage_rows:
        sample = usage_rows[0]
        raw_cols = [k for k in sample.keys() if k not in ("date", "Total", "week", "days")]
        style_names = [n for n, _, _ in PROVIDER_STYLE if n != "Total"]
        providers = [n for n in style_names if n in raw_cols]
        for c in sorted(raw_cols):
            if c not in providers:
                providers.append(c)
        weekly = aggregate_usage_weekly(usage_rows, providers)
        day_min, day_max_data, week_min, week_max_data, axis_max, week_labels = (
            _shared_axis_from_usage(usage_rows, weekly)
        )
        axis_meta = {
            "day_min": day_min,
            "day_max_data": day_max_data,
            "week_min": week_min,
            "week_max_data": week_max_data,
            "axis_max": axis_max,
        }

        # day mode: pad to axis_max with None (reserved future days)
        day_labels = _daily_labels(day_min, axis_max)
        day_by: dict[str, dict] = {r["date"]: r for r in usage_rows}

        def pack_usage_day() -> dict:
            series = {name: [] for name in providers + ["Total"]}
            for d in day_labels:
                row = day_by.get(d)
                for name in providers + ["Total"]:
                    if row is None:
                        series[name].append(None)
                    else:
                        series[name].append(round(float(row.get(name) or 0), 4))
            return {"labels": day_labels, "series": series}

        def pack_usage_week() -> dict:
            week_by = {r["date"]: r for r in weekly}
            series = {name: [] for name in providers + ["Total"]}
            for d in week_labels:
                row = week_by.get(d)
                for name in providers + ["Total"]:
                    if row is None:
                        series[name].append(None)
                    else:
                        series[name].append(round(float(row.get(name) or 0), 4))
            return {"labels": week_labels, "series": series}

        styles = {n: {"color": c, "stack": s} for n, c, s in PROVIDER_STYLE}
        for p in providers:
            styles.setdefault(p, {"color": "#888888", "stack": True})
        styles.setdefault("Total", {"color": "#111111", "stack": False})
        usage_payload = {
            "day": pack_usage_day(),
            "week": pack_usage_week(),
            "providers": providers,
            "styles": styles,
            "defaultMode": "week",
            "data_end": week_max_data,
        }
        if weekly:
            last_u = {
                "date": week_max_data,
                "value": round(float(weekly[-1].get("Total") or 0), 4),
            }

    # --- product (aligned to usage axis when available) ---
    if product_rows:
        day_map = {r["date"]: round(r["spend_usd_day"], 2) for r in product_rows}
        usage_by_date = {
            r["date"]: float(r["Total"])
            for r in usage_rows
            if r.get("date") and r.get("Total") not in (None, "")
        }
        if not usage_by_date:
            usage_by_date = {r["date"]: float(r["usage_total_b"]) for r in product_rows}
        price_for_week = {d: float(v) for d, v in price_by_date.items() if v is not None}
        week_rows_prod = aggregate_product_weekly(price_for_week, usage_by_date)
        week_map: dict[str, float] = {
            r["date"]: round(r["spend_usd_week"], 2) for r in week_rows_prod
        }

        if axis_max and day_min and week_labels:
            d_labels, d_vals = _pad_daily_map(day_map, day_min, axis_max)
            w_labels, w_vals = _pad_weekly_map(week_map, week_labels)
        else:
            d_labels = sorted(day_map)
            d_vals = [day_map[d] for d in d_labels]
            w_labels = sorted(week_map)
            w_vals = [week_map[d] for d in w_labels]

        product_payload = {
            "day": {
                "labels": d_labels,
                "values": d_vals,
                "unit": "USD / day (proxy)",
            },
            "week": {
                "labels": w_labels,
                "values": w_vals,
                "unit": "USD / week (proxy)",
            },
            "defaultMode": "week",
            "data_end": max(week_map) if week_map else None,
        }
        if week_map:
            xd = max(week_map)
            last_x = {"date": xd, "value": week_map[xd]}

    # --- price series aligned to chart② day domain when available ---
    if axis_max and day_min:
        p_labels, p_vals = _pad_daily_map(price_by_date, day_min, axis_max)
        price_series = [
            {"date": d, "value": v} for d, v in zip(p_labels, p_vals)
        ]
    else:
        price_series = [
            {"date": d, "value": v} for d, v in sorted(price_by_date.items()) if v is not None
        ]

    bundle = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "axis": axis_meta,
        "price": {"series": price_series, "n_official": n_official, "data_end": last_p["date"] if last_p else None},
        "usage": usage_payload,
        "product": product_payload,
        "summary": {"price": last_p, "usage_week_total": last_u, "spend_week": last_x},
    }
    data_json = json.dumps(bundle, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>LLM 市場三圖｜價格 × OpenRouter 用量 × 支出 proxy</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0b0b0c; --card: #141416; --line: #2a2a2e; --text: #e8e8ea; --muted: #9a9aa0;
    --accent: #22c9ee; --price: #f5a623; --spend: #7ee787;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text); }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 20px 18px 48px; }}
  header {{ margin-bottom: 16px; }}
  .headrow {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }}
  .headrow h1 {{ font-size: 22px; margin: 0 0 6px; font-weight: 600; }}
  .lang-switch {{ display: inline-flex; border: 1px solid #333; border-radius: 999px; overflow: hidden; flex-shrink: 0; }}
  .lang-switch button {{ border: 0; background: #1a1a1c; padding: 5px 12px; font-size: 12px; cursor: pointer; color: #aaa; }}
  .lang-switch button.on {{ background: #12b886; color: #fff; font-weight: 600; }}
  header .sub {{ color: var(--muted); font-size: 13px; line-height: 1.5; }}
  .nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 18px; }}
  .nav a {{
    color: var(--muted); text-decoration: none; font-size: 12px; padding: 6px 10px;
    border: 1px solid var(--line); border-radius: 999px; background: #111;
  }}
  .nav a:hover {{ color: #fff; border-color: #555; }}
  .summary {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px; margin-bottom: 18px;
  }}
  .summary .box {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px;
  }}
  .summary .k {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
  .summary .v {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
  .summary .d {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
  section.card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px 16px 12px; margin-bottom: 18px;
  }}
  section.card h2 {{ font-size: 16px; margin: 0 0 4px; font-weight: 600; }}
  section.card .desc {{ font-size: 12px; color: var(--muted); margin-bottom: 10px; line-height: 1.45; }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 8px; }}
  .seg {{ display: inline-flex; border: 1px solid #333; border-radius: 8px; overflow: hidden; }}
  .seg button {{ border: 0; background: #1a1a1c; padding: 6px 12px; font-size: 12px; cursor: pointer; color: #aaa; }}
  .seg button.on {{ background: #12b886; color: #fff; font-weight: 600; }}
  .act {{ display: inline-flex; gap: 6px; }}
  .act button {{
    border: 1px solid #444; background: #1a1a1c; padding: 6px 12px; font-size: 12px;
    cursor: pointer; border-radius: 8px; color: #ccc;
  }}
  .act button:hover {{ border-color: #12b886; color: #fff; }}
  .meta {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
  .meta b {{ color: #fff; }}
  .chart-box {{ position: relative; height: 360px; }}
  .chart-box.tall {{ height: 420px; }}
  .foot {{ font-size: 11px; color: #777; margin-top: 8px; line-height: 1.5; }}
  a {{ color: #7ec8e3; }}
  .page-foot {{
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 10px; padding: 0 2px 4px; color: var(--muted); font-size: 12px;
  }}
  .page-foot #traffic {{ cursor: pointer; user-select: none; }}
  .page-foot #traffic:hover {{ color: #ccc; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="headrow">
      <div>
        <h1 data-i18n="title">LLM 市場三圖儀表板</h1>
        <div class="sub">
          <span data-i18n="subtitle">① SDLLMTK 價格 · ② OpenRouter 各品牌用量（可點圖例顯示/取消 · 日/週）· ③ 價格×用量支出 proxy ·</span>
          <span data-i18n="updated">更新：</span><span id="upd"></span>
        </div>
      </div>
      <div class="lang-switch" id="langSwitch">
        <button type="button" data-lang="zh" class="on">中文</button>
        <button type="button" data-lang="en">EN</button>
      </div>
    </div>
    <div class="nav">
      <a href="#s1" data-i18n="nav1">① 價格</a>
      <a href="#s2" data-i18n="nav2">② 用量</a>
      <a href="#s3" data-i18n="nav3">③ 支出</a>
    </div>
    <div class="summary" id="summary"></div>
  </header>

  <section class="card" id="s1">
    <h2 data-i18n="h2price">① SDLLMTK 價格</h2>
    <div class="desc" data-i18n="descprice">USD / 百萬 tokens · 官方近7日 + 歷史錨點插值</div>
    <div class="meta" id="meta1"></div>
    <div class="chart-box"><canvas id="c1"></canvas></div>
    <div class="foot" id="foot1"></div>
  </section>

  <section class="card" id="s2">
    <h2 data-i18n="h2usage">② OpenRouter Token 使用量（多品牌）</h2>
    <div class="desc" data-i18n="descusage">OpenRouter Datasets · 點圖例顯示/取消 · 「全部取消」後再點一品牌可單看 · 預設週加總</div>
    <div class="toolbar">
      <div class="seg" id="mode2">
        <button type="button" data-mode="day" data-i18n="day">日</button>
        <button type="button" data-mode="week" class="on" data-i18n="week">週加總</button>
      </div>
      <div class="act">
        <button type="button" id="btn2AllOn" data-i18n="showall" data-i18n-title="showall_t">全部顯示</button>
        <button type="button" id="btn2AllOff" data-i18n="hideall" data-i18n-title="hideall_t">全部取消</button>
      </div>
    </div>
    <div class="meta" id="meta2"></div>
    <div class="chart-box tall"><canvas id="c2"></canvas></div>
    <div class="foot" data-i18n="footusage">單位 billion tokens。週 = 完整 ISO 週一～日 7 日加總，x 軸標籤為該週日；未滿週不進圖。Total 為全市場合計線。</div>
  </section>

  <section class="card" id="s3">
    <h2 data-i18n="h2spend">③ 市場支出規模 proxy（價格 × 使用量）</h2>
    <div class="desc" data-i18n="descspend">spend = SDLLMTK × usage_b × 1000 · 週模式用週均價 × 週用量合計</div>
    <div class="toolbar">
      <div class="seg" id="mode3">
        <button type="button" data-mode="day" data-i18n="day">日</button>
        <button type="button" data-mode="week" class="on" data-i18n="week">週加總</button>
      </div>
    </div>
    <div class="meta" id="meta3"></div>
    <div class="chart-box"><canvas id="c3"></canvas></div>
    <div class="foot" data-i18n="footspend">OpenRouter 表面用量 × 全市場混合單價，非全球真實營收、非百分比市占。</div>
  </section>
  <div class="page-foot">
    <div id="traffic" title="點一下切換：在線 → 本月 → 累計">在線—，今天—/峰值—</div>
  </div>
</div>
<script>
const D = {data_json};
document.getElementById('upd').textContent = D.updated_at || '';

const I18N = {{
  zh: {{
    title: 'LLM 市場三圖儀表板',
    subtitle: '① SDLLMTK 價格 · ② OpenRouter 各品牌用量（可點圖例顯示/取消 · 日/週）· ③ 價格×用量支出 proxy ·',
    updated: '更新：',
    nav1: '① 價格', nav2: '② 用量', nav3: '③ 支出',
    h2price: '① SDLLMTK 價格',
    descprice: 'USD / 百萬 tokens · 官方近7日 + 歷史錨點插值',
    h2usage: '② OpenRouter Token 使用量（多品牌）',
    descusage: 'OpenRouter Datasets · 點圖例顯示/取消 · 「全部取消」後再點一品牌可單看 · 預設週加總',
    day: '日', week: '週加總',
    showall: '全部顯示', hideall: '全部取消',
    showall_t: '顯示所有品牌 + Total', hideall_t: '取消全部，再點圖例單選一品牌',
    footusage: '單位 billion tokens。週 = 完整 ISO 週一～日 7 日加總，x 軸標籤為該週日；未滿週不進圖。Total 為全市場合計線。',
    h2spend: '③ 市場支出規模 proxy（價格 × 使用量）',
    descspend: 'spend = SDLLMTK × usage_b × 1000 · 週模式用週均價 × 週用量合計',
    footspend: 'OpenRouter 表面用量 × 全市場混合單價，非全球真實營收、非百分比市占。',
    sumPrice: '價格 SDLLMTK',
    sumUsage: '用量週合計 Total',
    sumSpend: '支出 proxy 週',
    sumWeekEnd: 'week end',
    sumUsd: 'USD',
    hoverHint: '滑過圖表看游標數值',
    hoverHint2: '滑過看各品牌數值 · 可全部取消後單選',
    noPrice: '無價格資料',
    noUsage: '無用量資料',
    noProduct: '無交叉資料',
    foot1: '官方 public_embed 點：',
    foot1b: ' · 其餘為圖表錨點/插值 · X 軸與②對齊（頭尾）',
    latest: 'Latest',
    mode: 'mode',
    axis: 'axis',
    n: 'n',
    docTitle: 'LLM 市場三圖｜價格 × OpenRouter 用量 × 支出 proxy',
    trafficTitle: '點一下切換：在線 → 本月 → 累計',
    trafficOnlinePrefix: '在線',
    trafficToday: '，今天',
    trafficPeak: '/峰值',
    trafficMonth: '本月 ',
    trafficMonthMid: ' 次 / ',
    trafficMonthEnd: ' 人',
    trafficTotal: '累計 ',
    trafficTotalMid: ' 次 / ',
    trafficTotalEnd: ' 人'
  }},
  en: {{
    title: 'LLM Market Triple Dashboard',
    subtitle: '① SDLLMTK price · ② OpenRouter usage by brand (click legend · day/week) · ③ Price×usage spend proxy ·',
    updated: 'Updated: ',
    nav1: '① Price', nav2: '② Usage', nav3: '③ Spend',
    h2price: '① SDLLMTK Price',
    descprice: 'USD / million tokens · official ~7d window + historical anchor interpolation',
    h2usage: '② OpenRouter Token Usage (multi-brand)',
    descusage: 'OpenRouter Datasets · click legend to show/hide · after “Hide all”, click one brand to solo · default weekly sum',
    day: 'Day', week: 'Weekly sum',
    showall: 'Show all', hideall: 'Hide all',
    showall_t: 'Show all brands + Total', hideall_t: 'Hide all, then click one legend to solo',
    footusage: 'Unit: billion tokens. Week = full ISO Mon–Sun 7-day sum; x-axis label is that Sunday; partial weeks omitted. Total is the market-wide line.',
    h2spend: '③ Market spend-size proxy (price × usage)',
    descspend: 'spend = SDLLMTK × usage_b × 1000 · weekly mode uses week-avg price × week usage sum',
    footspend: 'OpenRouter surface usage × blended market unit price — not real global revenue, not share %.',
    sumPrice: 'Price SDLLMTK',
    sumUsage: 'Usage weekly Total',
    sumSpend: 'Spend proxy (week)',
    sumWeekEnd: 'week end',
    sumUsd: 'USD',
    hoverHint: 'hover chart for crosshair values',
    hoverHint2: 'hover for per-brand values · hide all then click one to solo',
    noPrice: 'No price data',
    noUsage: 'No usage data',
    noProduct: 'No cross data',
    foot1: 'Official public_embed points: ',
    foot1b: ' · other points are chart anchors/interpolation · X-axis aligned with chart ②',
    latest: 'Latest',
    mode: 'mode',
    axis: 'axis',
    n: 'n',
    docTitle: 'LLM Market Triple Dashboard',
    trafficTitle: 'Click to cycle: online → month → total',
    trafficOnlinePrefix: 'Online ',
    trafficToday: ' · today ',
    trafficPeak: ' / peak ',
    trafficMonth: 'This month ',
    trafficMonthMid: ' hits / ',
    trafficMonthEnd: ' visitors',
    trafficTotal: 'All-time ',
    trafficTotalMid: ' hits / ',
    trafficTotalEnd: ' visitors'
  }}
}};

let LANG = localStorage.getItem('llm_dash_lang') || 'zh';
function t(key) {{ return (I18N[LANG] && I18N[LANG][key]) || I18N.zh[key] || key; }}

function applyStaticI18n() {{
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const k = el.getAttribute('data-i18n');
    if (k && I18N[LANG][k]) el.textContent = I18N[LANG][k];
  }});
  document.querySelectorAll('[data-i18n-title]').forEach(el => {{
    const k = el.getAttribute('data-i18n-title');
    if (k && I18N[LANG][k]) el.title = I18N[LANG][k];
  }});
  document.title = t('docTitle');
  document.documentElement.lang = LANG === 'en' ? 'en' : 'zh-Hant';
  document.querySelectorAll('#langSwitch button').forEach(b => b.classList.toggle('on', b.dataset.lang === LANG));
  const trEl = document.getElementById('traffic');
  if (trEl) trEl.title = t('trafficTitle');
}}

function setLang(lang) {{
  LANG = lang;
  localStorage.setItem('llm_dash_lang', lang);
  applyStaticI18n();
  renderSummary();
  renderMeta1();
  renderMeta2();
  renderMeta3();
  renderFoot1();
  paintTraffic(null);
}}

document.getElementById('langSwitch').addEventListener('click', e => {{
  const b = e.target.closest('button[data-lang]');
  if (b && b.dataset.lang !== LANG) setLang(b.dataset.lang);
}});

/** 垂直游標線：滑過任一圖都會畫十字參考線 */
const crosshairPlugin = {{
  id: 'crosshair',
  afterDatasetsDraw(chart) {{
    const active = chart.tooltip && chart.tooltip.getActiveElements
      ? chart.tooltip.getActiveElements()
      : (chart.tooltip && chart.tooltip._active) || [];
    if (!active || !active.length) return;
    const x = active[0].element.x;
    const {{ top, bottom }} = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = 'rgba(255,255,255,0.45)';
    ctx.stroke();
    ctx.restore();
  }}
}};
Chart.register(crosshairPlugin);

const tipBase = {{
  enabled: true,
  mode: 'index',
  intersect: false,
  position: 'nearest',
  backgroundColor: 'rgba(20,20,24,0.94)',
  titleColor: '#fff',
  bodyColor: '#e8e8ea',
  borderColor: '#444',
  borderWidth: 1,
  padding: 10,
  displayColors: true,
  boxPadding: 4,
  titleFont: {{ size: 13, weight: '600' }},
  bodyFont: {{ size: 12 }},
  callbacks: {{
    title: (items) => items.length ? String(items[0].label) : ''
  }}
}};

const hoverBase = {{
  mode: 'index',
  intersect: false
}};

function fmtNum(v, digits) {{
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, {{ maximumFractionDigits: digits ?? 1 }});
  return n.toLocaleString(undefined, {{ maximumFractionDigits: digits ?? 4 }});
}}

function renderSummary() {{
  const s = D.summary || {{}};
  const boxes = [];
  if (s.price) boxes.push('<div class="box"><div class="k">' + t('sumPrice') + '</div><div class="v" style="color:var(--price)">' + Number(s.price.value).toFixed(4) + '</div><div class="d">' + s.price.date + '</div></div>');
  if (s.usage_week_total) boxes.push('<div class="box"><div class="k">' + t('sumUsage') + '</div><div class="v" style="color:var(--accent)">' + Number(s.usage_week_total.value).toLocaleString(undefined,{{maximumFractionDigits:0}}) + ' b</div><div class="d">' + t('sumWeekEnd') + ' ' + s.usage_week_total.date + '</div></div>');
  if (s.spend_week) boxes.push('<div class="box"><div class="k">' + t('sumSpend') + '</div><div class="v" style="color:var(--spend)">' + Number(s.spend_week.value).toLocaleString(undefined,{{maximumFractionDigits:0}}) + '</div><div class="d">' + t('sumUsd') + ' · ' + s.spend_week.date + '</div></div>');
  document.getElementById('summary').innerHTML = boxes.join('');
}}

// last non-null index (axis may pad trailing empty points)
function lastDefined(arr) {{
  if (!arr || !arr.length) return -1;
  for (let i = arr.length - 1; i >= 0; i--) {{
    if (arr[i] != null && arr[i] !== '') return i;
  }}
  return -1;
}}

// ① price
let priceSeries = [];
let priceLast = null;
let priceAxisNote = '';
function renderMeta1() {{
  const el = document.getElementById('meta1');
  if (!priceSeries.length) {{ el.textContent = t('noPrice'); return; }}
  el.innerHTML = priceLast
    ? t('latest') + ' <b>' + Number(priceLast.value).toFixed(4) + '</b> · ' + priceLast.date + ' · ' + t('n') + '=' + priceSeries.length + priceAxisNote + ' · <span style="color:#888">' + t('hoverHint') + '</span>'
    : t('noPrice');
}}
function renderFoot1() {{
  document.getElementById('foot1').textContent = t('foot1') + (D.price.n_official || 0) + t('foot1b');
}}
(function() {{
  const series = (D.price && D.price.series) || [];
  if (!series.length) return;
  priceSeries = series;
  const vals = series.map(r => r.value);
  const li = lastDefined(vals);
  priceLast = li >= 0 ? series[li] : null;
  priceAxisNote = (D.axis && D.axis.axis_max) ? ' · ' + t('axis') + '→' + D.axis.axis_max : '';
  renderMeta1();
  renderFoot1();
  new Chart(document.getElementById('c1'), {{
    type: 'line',
    data: {{
      labels: series.map(r => r.date),
      datasets: [{{
        label: 'SDLLMTK',
        data: vals,
        borderColor: '#f5a623', backgroundColor: 'rgba(245,166,35,.12)',
        fill: true, tension: 0.2, borderWidth: 2, spanGaps: false,
        pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 16
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: hoverBase,
      hover: hoverBase,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          ...tipBase,
          callbacks: {{
            ...tipBase.callbacks,
            label: (ctx) => ` SDLLMTK: ${{fmtNum(ctx.parsed.y, 4)}} USD/M tokens`
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#888', maxTicksLimit: 12, maxRotation: 0 }}, grid: {{ color: '#ffffff10' }} }},
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#ffffff14' }},
             title: {{ display: true, text: 'USD / M tokens', color: '#aaa' }} }}
      }}
    }}
  }});
}})();

// ② usage multi
let chart2 = null;
let usageMode = 'week';
const hiddenUsageLabels = new Set();
function buildUsageDatasets(mode) {{
  const U = D.usage;
  if (!U) return null;
  const block = U[mode];
  const orderStack = U.providers.slice();
  const styles = U.styles || {{}};
  const ds = [];
  for (const name of orderStack) {{
    const st = styles[name] || {{ color: '#888' }};
    ds.push({{
      label: name,
      data: block.series[name] || [],
      borderColor: st.color,
      backgroundColor: st.color,
      borderWidth: 0.5,
      pointRadius: 0,
      pointHoverRadius: 4,
      pointHitRadius: 12,
      fill: true,
      tension: 0.05,
      stack: 'usage',
      order: 2
    }});
  }}
  ds.push({{
    label: 'Total',
    data: block.series.Total || [],
    borderColor: '#e8e8ea',
    backgroundColor: 'transparent',
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 5,
    pointHitRadius: 12,
    fill: false,
    tension: 0.05,
    yAxisID: 'yTotal',
    order: 0
  }});
  return {{ labels: block.labels, datasets: ds }};
}}
function syncHiddenUsage() {{
  if (!chart2) return;
  hiddenUsageLabels.clear();
  chart2.data.datasets.forEach((ds, i) => {{
    if (!chart2.isDatasetVisible(i)) hiddenUsageLabels.add(ds.label);
  }});
}}
function applyHiddenUsage() {{
  if (!chart2) return;
  chart2.data.datasets.forEach((ds, i) => {{
    chart2.setDatasetVisibility(i, !hiddenUsageLabels.has(ds.label));
  }});
  chart2.update();
  const yScale = chart2.scales.y;
  if (yScale && chart2.scales.yTotal) {{
    chart2.scales.yTotal.options.max = yScale.max;
    chart2.update('none');
  }}
}}
function setAllUsageVisible(on) {{
  if (!chart2) return;
  hiddenUsageLabels.clear();
  if (!on) {{
    chart2.data.datasets.forEach(ds => hiddenUsageLabels.add(ds.label));
  }}
  applyHiddenUsage();
}}
function renderMeta2() {{
  const U = D.usage;
  const el = document.getElementById('meta2');
  if (!U) {{ el.textContent = t('noUsage'); return; }}
  const block = U[usageMode];
  const tot = block.series.Total || [];
  const li = lastDefined(tot);
  const last = li >= 0 ? tot[li] : null;
  const end = li >= 0 ? block.labels[li] : (U.data_end || '');
  const axisEnd = block.labels[block.labels.length - 1] || '';
  el.innerHTML = t('latest') + ' Total <b>' + (last != null ? Number(last).toLocaleString(undefined,{{maximumFractionDigits:1}}) : '—') + '</b> · ' + end + ' · ' + t('mode') + '=' + usageMode + ' · ' + t('n') + '=' + block.labels.length + ' · ' + t('axis') + '→' + axisEnd + ' · <span style="color:#888">' + t('hoverHint2') + '</span>';
}}
function renderUsage(mode) {{
  const U = D.usage;
  if (!U) {{
    document.getElementById('meta2').textContent = t('noUsage');
    return;
  }}
  usageMode = mode;
  document.querySelectorAll('#mode2 button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  const data = buildUsageDatasets(mode);
  if (data && data.datasets) {{
    data.datasets.forEach(ds => {{ ds.spanGaps = false; }});
  }}
  renderMeta2();
  if (chart2) chart2.destroy();
  chart2 = new Chart(document.getElementById('c2'), {{
    type: 'line',
    data,
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: hoverBase,
      hover: hoverBase,
      plugins: {{
        legend: {{
          display: true,
          position: 'bottom',
          labels: {{
            usePointStyle: true, pointStyle: 'circle', boxWidth: 9, padding: 12,
            color: '#ccc', font: {{ size: 11 }}
          }},
          onClick(e, legendItem, legend) {{
            const ch = legend.chart;
            const idx = legendItem.datasetIndex;
            ch.setDatasetVisibility(idx, !ch.isDatasetVisible(idx));
            ch.update();
            syncHiddenUsage();
            const yScale = ch.scales.y;
            if (yScale && ch.scales.yTotal) {{
              ch.scales.yTotal.options.max = yScale.max;
              ch.update('none');
            }}
          }}
        }},
        tooltip: {{
          ...tipBase,
          itemSort: (a, b) => (b.parsed.y || 0) - (a.parsed.y || 0),
          filter: (item) => item.parsed.y != null && item.parsed.y !== 0 || item.dataset.label === 'Total',
          callbacks: {{
            ...tipBase.callbacks,
            label: (ctx) => ` ${{ctx.dataset.label}}: ${{fmtNum(ctx.parsed.y, 2)}} b`,
            footer: (items) => {{
              let brandSum = 0;
              items.forEach(it => {{ if (it.dataset.label !== 'Total') brandSum += Number(it.parsed.y) || 0; }});
              return 'visible brands sum: ' + fmtNum(brandSum, 1) + ' b';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ maxTicksLimit: 14, maxRotation: 0, color: '#888' }}, grid: {{ color: '#ffffff10' }} }},
        y: {{
          stacked: true, beginAtZero: true,
          title: {{ display: true, text: mode === 'week' ? 'b tokens / week' : 'b tokens / day', color: '#aaa' }},
          ticks: {{ color: '#888' }}, grid: {{ color: '#ffffff12' }}
        }},
        yTotal: {{ stacked: false, beginAtZero: true, display: false, grid: {{ drawOnChartArea: false }}, min: 0 }}
      }}
    }}
  }});
  applyHiddenUsage();
}}
document.getElementById('mode2').addEventListener('click', e => {{
  const b = e.target.closest('button[data-mode]');
  if (b) renderUsage(b.dataset.mode);
}});
document.getElementById('btn2AllOn').addEventListener('click', () => setAllUsageVisible(true));
document.getElementById('btn2AllOff').addEventListener('click', () => setAllUsageVisible(false));

// ③ product
let chart3 = null;
let productMode = 'week';
function renderMeta3() {{
  const P = D.product;
  const el = document.getElementById('meta3');
  if (!P) {{ el.textContent = t('noProduct'); return; }}
  const block = P[productMode];
  const li = lastDefined(block.values);
  const last = li >= 0 ? block.values[li] : null;
  const end = li >= 0 ? block.labels[li] : (P.data_end || '');
  const axisEnd = block.labels[block.labels.length - 1] || '';
  el.innerHTML = t('latest') + ' <b>' + (last != null ? Number(last).toLocaleString() : '—') + '</b> · ' + end + ' · ' + block.unit + ' · ' + t('n') + '=' + block.labels.length + ' · ' + t('axis') + '→' + axisEnd + ' · <span style="color:#888">' + t('hoverHint') + '</span>';
}}
function renderProduct(mode) {{
  const P = D.product;
  if (!P) {{
    document.getElementById('meta3').textContent = t('noProduct');
    return;
  }}
  productMode = mode;
  document.querySelectorAll('#mode3 button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  const block = P[mode];
  renderMeta3();
  if (chart3) chart3.destroy();
  chart3 = new Chart(document.getElementById('c3'), {{
    type: 'line',
    data: {{
      labels: block.labels,
      datasets: [{{
        label: 'Spend proxy',
        data: block.values,
        borderColor: '#7ee787', backgroundColor: 'rgba(126,231,135,.12)',
        fill: true, tension: 0.2, borderWidth: 2, spanGaps: false,
        pointRadius: 0, pointHoverRadius: 5, pointHitRadius: 16
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: hoverBase,
      hover: hoverBase,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          ...tipBase,
          callbacks: {{
            ...tipBase.callbacks,
            label: (ctx) => ` Spend: ${{fmtNum(ctx.parsed.y, 0)}} ${{block.unit}}`
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color: '#888', maxTicksLimit: 12, maxRotation: 0 }}, grid: {{ color: '#ffffff10' }} }},
        y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#ffffff14' }},
             title: {{ display: true, text: block.unit, color: '#aaa' }} }}
      }}
    }}
  }});
}}
document.getElementById('mode3').addEventListener('click', e => {{
  const b = e.target.closest('button[data-mode]');
  if (b) renderProduct(b.dataset.mode);
}});

applyStaticI18n();
renderSummary();
renderUsage((D.usage && D.usage.defaultMode) || 'week');
renderProduct((D.product && D.product.defaultMode) || 'week');

const LIVE = 'https://wtx.19850926.xyz/';
const TRAFFIC_SITE = 'llm';
const SID_KEY = 'llmDashSid';
const TRAFFIC_MODES = ['online', 'month', 'total'];
let trafficMode = 0;
let lastTraffic = null;
function visitorSid() {{
  try {{
    let s = localStorage.getItem(SID_KEY);
    if (s && /^[a-zA-Z0-9_-]{{8,64}}$/.test(s)) return s;
    s = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(SID_KEY, s);
    return s;
  }} catch (e) {{
    return 't' + Date.now().toString(36) + 'x';
  }}
}}
function paintTraffic(st) {{
  if (st) lastTraffic = st;
  const el = document.getElementById('traffic');
  const s = lastTraffic;
  if (!el) return;
  if (!s) {{
    el.textContent = t('trafficOnlinePrefix') + '—' + t('trafficToday') + '—' + t('trafficPeak') + '—';
    return;
  }}
  const mode = TRAFFIC_MODES[trafficMode] || 'online';
  const d = '—';
  if (mode === 'month') {{
    el.textContent = t('trafficMonth') + (s.monthPv != null ? s.monthPv : d) + t('trafficMonthMid') + (s.monthUv != null ? s.monthUv : d) + t('trafficMonthEnd');
  }} else if (mode === 'total') {{
    el.textContent = t('trafficTotal') + (s.totalPv != null ? s.totalPv : d) + t('trafficTotalMid') + (s.totalUv != null ? s.totalUv : d) + t('trafficTotalEnd');
  }} else {{
    el.textContent = t('trafficOnlinePrefix') + (s.online != null ? s.online : d) + t('trafficToday') + (s.uv != null ? s.uv : d) + t('trafficPeak') + (s.peak != null ? s.peak : d);
  }}
}}
async function trafficPing(isHit) {{
  try {{
    const sid = visitorSid();
    const q = LIVE + '?kind=ping&site=' + encodeURIComponent(TRAFFIC_SITE) + '&sid=' + encodeURIComponent(sid) + (isHit ? '&hit=1' : '');
    const st = await fetch(q, {{ cache: 'no-store' }}).then(r => r.ok ? r.json() : null);
    if (st && st.ok) paintTraffic(st);
  }} catch (e) {{}}
}}
function startTraffic() {{
  const el = document.getElementById('traffic');
  if (el) {{
    el.addEventListener('click', () => {{
      trafficMode = (trafficMode + 1) % TRAFFIC_MODES.length;
      paintTraffic(null);
    }});
  }}
  trafficPing(true);
  setInterval(() => {{
    if (document.visibilityState === 'visible') trafficPing(false);
  }}, 30000);
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'visible') trafficPing(false);
  }});
}}
startTraffic();

</script>
</body>
</html>
"""

    CHART_DASHBOARD.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-mm", action="store_true", help="do not fetch MacroMicro")
    ap.add_argument("--import-mm", default="", help="import chart_data_raw.json path")
    ap.add_argument("--skip-price", action="store_true")
    args = ap.parse_args()

    # 1 price
    price_rows = load_price_rows()
    if not args.skip_price:
        print("Fetching SDLLMTK public window...")
        window = fetch_sdllmtk_window()
        for d, v in sorted(window.items()):
            print(f"  price {d} {v:.4f}")
        backup(PRICE_CSV)
        price_rows = merge_price(price_rows, window)
        save_price(price_rows)
        print("price rows", len(price_rows))
    price_daily = price_series_daily(price_rows)

    # 2 usage
    usage_rows: list[dict] = []
    providers: list[str] = []
    if args.import_mm:
        path = Path(args.import_mm)
        print("Import MM", path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_prov = parse_mm_payload(payload)
        usage_rows, providers = usage_to_rows(by_prov)
        backup(USAGE_CSV)
        save_usage(usage_rows, providers)
        print("usage days", len(usage_rows), "last Total", usage_rows[-1]["Total"])
    elif not args.skip_mm:
        print("Fetching MacroMicro (headed browser, may flash a window)...")
        payload = fetch_mm_via_playwright()
        raw_path = DIR / "openrouter_usage_raw.json"
        raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        by_prov = parse_mm_payload(payload)
        usage_rows, providers = usage_to_rows(by_prov)
        backup(USAGE_CSV)
        save_usage(usage_rows, providers)
        print("usage days", len(usage_rows), "last", usage_rows[-1]["date"], usage_rows[-1]["Total"])
    else:
        # load existing
        if USAGE_CSV.exists():
            with USAGE_CSV.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                providers = [c for c in (reader.fieldnames or []) if c not in ("date", "Total")]
                usage_rows = list(reader)
            print("loaded usage", len(usage_rows))
        else:
            print("WARN no usage CSV")

    usage_total = load_usage_total()

    # 3 product
    product_rows = build_product(price_daily, usage_total)
    backup(PRODUCT_CSV)
    save_product(product_rows)
    print("product overlap days", len(product_rows))
    if product_rows:
        last = product_rows[-1]
        print(
            "last product",
            last["date"],
            f"price={last['price_sdllmtk']:.4f}",
            f"usage_b={last['usage_total_b']:.2f}",
            f"spend_usd_day={last['spend_usd_day']:,.0f}",
        )

    # charts
    write_price_chart(price_daily, price_rows)
    if usage_rows:
        write_usage_chart(usage_rows)
    write_product_chart(product_rows, price_daily, usage_total)
    write_combined_dashboard(price_daily, price_rows, usage_rows, product_rows)

    meta = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "price_points": len(price_rows),
        "price_daily": len(price_daily),
        "usage_days": len(usage_total),
        "product_days": len(product_rows),
        "last_price": price_rows[-1] if price_rows else None,
        "last_usage_total": (
            {"date": usage_rows[-1]["date"], "Total": usage_rows[-1]["Total"]} if usage_rows else None
        ),
        "last_product": product_rows[-1] if product_rows else None,
        "formula": "spend_usd_day = SDLLMTK * usage_total_b * 1000",
        "charts": [
            str(CHART_DASHBOARD),
            str(CHART_PRICE),
            str(CHART_USAGE),
            str(CHART_PRODUCT),
        ],
        "dashboard": str(CHART_DASHBOARD),
    }
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS charts written")
    print(" DASHBOARD", CHART_DASHBOARD)
    print(" ", CHART_PRICE)
    print(" ", CHART_USAGE)
    print(" ", CHART_PRODUCT)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("FAIL", type(e).__name__, e)
        sys.exit(1)
