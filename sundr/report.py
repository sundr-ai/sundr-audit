"""Render a Savings Audit as a self-contained HTML report."""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, select_autoescape

from .audit import AuditResult

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Savings Audit · {{ r.task.name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap">
<style>
  :root { --paper:#f4f1ea; --card:#fbfaf6; --ink:#111417; --ink-2:#2c3238; --muted:#5d636a; --faint:#8b9097; --rule:#d9d4c8; --rule-soft:#e5e1d7;
          --signal:#2a44e0; --signal-soft:#e3e7fb; --series-sundr:#3953e6; --series-base:#a8762a; --good:#1f7a4d;
          --sans:"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; --mono:"Geist Mono", ui-monospace, Menlo, monospace; }
  @media (prefers-color-scheme: dark) { :root { --paper:#111417; --card:#181c20; --ink:#eceae4; --ink-2:#c9cbc7; --muted:#9aa0a6; --faint:#767c82;
          --rule:#2a2f35; --rule-soft:#22272c; --signal:#9db0ff; --signal-soft:#1c2340; --series-sundr:#6f86f2; --series-base:#b5843a; --good:#5fbf8a; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font:400 15.5px/1.6 var(--sans); -webkit-font-smoothing:antialiased; }
  main { max-width:920px; margin:0 auto; padding:40px 24px 88px; }
  .top { display:flex; justify-content:space-between; align-items:center; gap:16px; padding-bottom:28px; border-bottom:1px solid var(--rule); }
  .brand { display:inline-flex; gap:10px; align-items:center; font-weight:600; font-size:19px; letter-spacing:-.03em; color:var(--ink); text-decoration:none; }
  .brand svg { width:26px; height:19px; }
  .eyebrow { font:500 11.5px/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
  h1 { font-size:34px; line-height:1.08; letter-spacing:-.035em; margin:36px 0 10px; font-weight:600; }
  h2 { font-size:19px; letter-spacing:-.015em; margin:56px 0 14px; font-weight:600; }
  .meta { color:var(--muted); font:400 12.5px/1.6 var(--mono); }
  .headline { margin-top:28px; padding:28px 0 24px; border-top:1px solid var(--ink); border-bottom:1px solid var(--rule); }
  .headline .big { font-size:27px; font-weight:600; letter-spacing:-.025em; line-height:1.25; max-width:30em; }
  .headline .big em { font-style:normal; color:var(--signal); }
  .headline .sub { color:var(--muted); margin-top:10px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); border-bottom:1px solid var(--rule); }
  .tile { padding:20px 20px 18px 0; }
  .tile + .tile { padding-left:20px; border-left:1px solid var(--rule); }
  .tile .v { font-size:26px; font-weight:600; letter-spacing:-.03em; }
  .tile .k { color:var(--muted); font-size:13px; margin-top:4px; }
  table { width:100%; border-collapse:collapse; font-size:14.5px; }
  th { text-align:left; font:500 11px/1.2 var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--muted); padding:10px 12px; border-bottom:1px solid var(--ink); }
  td { padding:11px 12px; border-bottom:1px solid var(--rule); vertical-align:top; }
  tr:last-child td { border-bottom:0; }
  td.num, th.num { text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }
  .card { background:var(--card); border:1px solid var(--rule); border-radius:10px; padding:24px; }
  .legend { display:flex; gap:20px; font-size:13.5px; color:var(--ink-2); margin-bottom:10px; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:8px; vertical-align:-1px; }
  .chart svg { width:100%; height:auto; display:block; overflow:visible; }
  .chart .grid line { stroke:var(--rule-soft); }
  .chart .tick { fill:var(--muted); font:400 11.5px var(--mono); }
  .chart .val { fill:var(--ink); font:600 12px var(--mono); }
  .note { color:var(--muted); font-size:13px; margin-top:10px; }
  .pill { display:inline-block; font:500 10.5px/1 var(--mono); letter-spacing:.06em; text-transform:uppercase; padding:4px 7px; border:1px solid var(--rule); border-radius:999px; color:var(--muted); }
  code { font:500 13px var(--mono); }
  .cta { margin-top:56px; padding:24px 28px; border-radius:10px; background:var(--ink); color:var(--paper); display:flex; justify-content:space-between; gap:20px; align-items:center; flex-wrap:wrap; }
  .cta a { color:var(--paper); font-weight:500; }
  @media (prefers-color-scheme: dark) { .cta { background:var(--card); border:1px solid var(--rule); color:var(--ink); } .cta a { color:var(--signal); } }
  .wrap-x { overflow-x:auto; }
  @media (max-width:640px) { .tile + .tile { padding-left:0; border-left:0; border-top:1px solid var(--rule); } .chart svg { min-width:560px; } }
</style>
</head>
<body><main>
<div class="top">
  <a class="brand" href="https://sundr.ai"><svg viewBox="0 0 28 20" fill="none" aria-hidden="true"><path d="M1 10h6M7 10c3 0 3-6 7-6s4 6 7 6M7 10h14M7 10c3 0 3 6 7 6s4-6 7-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="24.5" cy="10" r="2.6" fill="#2a44e0"/></svg>sundr</a>
  <span class="eyebrow">Savings Audit</span>
</div>
<h1>{{ r.task.name }}</h1>
<div class="meta">{{ r.n_records | thousands }} logged calls · {{ r.backends | join(" · ") }} · {{ r.generated_at }}</div>

<div class="headline">
{% if top %}
  <div class="big">At a {{ top.epsilon | pct }} error budget, {{ top.label }} could answer <em>{{ top.coverage | pct0 }}</em> of these calls{% if top.net %}, saving you {{ top.net | usd }} a month after fees{% endif %}.</div>
  <div class="sub">Certified with 95% confidence on {{ r.n_calibration | thousands }} held-out calls. Every other call would still go to your current model.</div>
{% else %}
  <div class="big">No error budget could be certified on this data yet.</div>
  <div class="sub">Usually this means too few logged calls, or a task the decision models can't separate. See “Data needed” below.</div>
{% endif %}
</div>

<div class="tiles">
  <div class="tile"><div class="v">{{ r.agreement_all | pct1 }}</div><div class="k">agreement with your judge, held-out calls</div></div>
  {% if r.baseline_agreement_all is not none %}<div class="tile"><div class="v">{{ r.baseline_agreement_all | pct1 }}</div><div class="k">asking the decision model one direct question</div></div>{% endif %}
  <div class="tile"><div class="v">{{ (r.decision_usd_per_call * 1000) | usd4 }}</div><div class="k">decision-model cost per 1,000 calls</div></div>
  {% if r.latency_p50_ms %}<div class="tile"><div class="v">{{ r.latency_p50_ms | int }} ms</div><div class="k">median latency (p95 {{ r.latency_p95_ms | int }} ms)</div></div>{% endif %}
</div>

{% if r.gold %}
<h2>Against your human labels</h2>
<div class="card">
  <p>On {{ "{:,}".format(r.gold.n) }} held-out calls with a human label, your LLM's verdict was wrong on <strong>{{ r.gold.llm_error | pct1 }}</strong> and Sundr's on <strong>{{ r.gold.sundr_error | pct1 }}</strong>.{% if r.gold.handled_error is not none %} On the {{ "{:,}".format(r.gold.handled_n) }} of those Sundr would answer at the headline budget, Sundr was wrong on <strong>{{ r.gold.handled_error | pct1 }}</strong>.{% endif %}</p>
  <div class="note">The certificate is about agreement with your LLM. These numbers show how both compare with ground truth, on a sample too small for a certificate of its own unless you send thousands of labels.</div>
</div>
{% endif %}

{% if r.candidates %}
<h2>Which model can take over</h2>
<p class="meta">Each option is certified separately against your current model's verdicts on the same held-out calls. “Switch everything” is the plain model swap; the certified share is how much it can answer with a threshold Sundr checks continuously.</p>
<div class="wrap-x"><table>
<tr><th>Option</th><th class="num">Agreement</th><th class="num">Switch everything</th>{% for eps in budgets %}<th class="num">Certified at {{ eps | pct }}</th>{% endfor %}<th class="num">Cost / 1,000 calls</th>{% if r.monthly_calls %}<th class="num">Net / month at {{ preferred | pct }}</th>{% endif %}</tr>
{% for o in options %}
<tr><td>{{ o.label }}{% if o.note %}<div class="meta">{{ o.note }}</div>{% endif %}</td>
<td class="num">{{ o.agreement | pct1 }}</td>
<td class="num">{% if o.switch_bound is none %}—{% elif o.switch_bound <= preferred %}safe at {{ preferred | pct }}{% else %}up to {{ o.switch_bound | pct1 }} off{% endif %}</td>
{% for eps in budgets %}<td class="num">{{ o.coverage[eps] | pct0 }}</td>{% endfor %}
<td class="num">{{ (o.cost * 1000) | usd4 }}</td>
{% if r.monthly_calls %}<td class="num">{{ o.net | usd }}</td>{% endif %}</tr>
{% endfor %}
</table></div>
{% if r.candidate_notes %}<div class="note">Not evaluated: {% for model, reason in r.candidate_notes %}{{ model }} ({{ reason }}){% if not loop.last %}; {% endif %}{% endfor %}.</div>{% endif %}
<div class="note">Candidate LLMs were asked with a generic prompt built from the task description. In shadow mode Sundr re-checks them on your own prompts before anything is switched.</div>
{% endif %}

<h2>Certified coverage by error budget</h2>
<div class="card chart">
  <div class="legend" aria-hidden="true"><span><i style="background:var(--series-sundr)"></i>Sundr</span><span><i style="background:var(--series-base)"></i>One question</span></div>
  <div class="wrap-x">{{ chart | safe }}</div>
  <div class="note">Coverage is the share of calls Sundr answers itself. “One question” asks the decision model for the verdict directly, without splitting it.</div>
</div>

<h2>By budget</h2>
<div class="wrap-x"><table>
<tr><th>Error budget</th><th class="num">Calls answered</th><th class="num">Certified bound</th><th class="num">One question</th>{% if r.monthly_calls %}<th class="num">Savings / month</th><th class="num">Fee</th><th class="num">Net to you</th>{% endif %}</tr>
{% for row in r.rows %}
<tr><td>{{ row.epsilon | pct }}</td>
<td class="num">{{ row.coverage | pct0 }}</td>
<td class="num">{% if row.certificate %}≤ {{ row.certificate.upper_bound | pct2 }}{% else %}—{% endif %}</td>
<td class="num">{{ row.baseline_coverage | pct0 }}</td>
{% if r.monthly_calls %}<td class="num">{{ row.monthly_savings_usd | usd }}</td><td class="num">{{ row.monthly_fee_usd | usd }}</td><td class="num">{{ row.monthly_net_usd | usd }}</td>{% endif %}</tr>
{% endfor %}
</table></div>
{% if r.monthly_calls %}<div class="note">Assumes {{ r.monthly_calls | thousands }} calls a month at {{ r.usd_per_call | usd4 }} each, the Takeover plan's {{ (r.fee_share * 100) | round | int }}% fee on savings, and about 3% of answered calls still sent to your model as an ongoing audit.</div>{% endif %}

<h2>Data needed</h2>
<p>Certifying a budget reliably takes roughly 120 ÷ budget held-out calls: about 12,000 for 1% and 6,000 for 2%. This audit held out {{ r.n_calibration | thousands }}.{% if r.n_calibration < 12000 %} More logged calls, or a few days of shadow mode, raise certified coverage at tight budgets.{% endif %}</p>

<h2>Questions Sundr asks</h2>
<p class="meta">{{ r.questions_kept | length }} kept of {{ r.questions_drafted }} drafted · each answered by every decision model, choice questions in two option orders</p>
<div class="wrap-x"><table>
<tr><th>Name</th><th>Type</th><th>Question</th></tr>
{% for q in r.questions_kept %}
<tr><td><code>{{ q.name }}</code></td><td><span class="pill">{{ q.type }}</span></td><td>{{ q.instructions }}{% if q.options %}<div class="meta">{{ q.options | join(" · ") }}</div>{% endif %}</td></tr>
{% endfor %}
</table></div>

<h2>How this was measured</h2>
<p>{{ r.n_train | thousands }} calls trained a small logistic-regression combiner over the decision models' answers. Its threshold was certified on {{ r.n_calibration | thousands }} separate calls with Learn then Test (fixed-sequence binomial tests, 95% confidence). The bound holds for future traffic that resembles these logs; in production a live audit slice re-checks it and hands traffic back to your model if it slips.</p>
<p class="meta">Labels seen: {% for k, v in r.label_counts.items() %}{{ k }} ({{ v | thousands }}){% if not loop.last %} · {% endif %}{% endfor %}</p>

<div class="cta"><span><strong>Next:</strong> a 30-day free shadow run. One base-URL change; nothing changes for your users until a budget is certified on live traffic.</span><a href="mailto:hello@sundr.ai?subject=Shadow%20run">hello@sundr.ai →</a></div>
</main></body></html>
"""


def _column(cx: float, value: float, base_y: float, plot_h: float, width: float, cls: str, tip: str) -> str:
    """A column with a 4px rounded data-end, square at the baseline."""
    h = max(plot_h * value, 1.5)
    y0, x0, x1 = base_y - h, cx - width / 2, cx + width / 2
    r = min(4.0, h / 2)
    d = (f"M{x0:.1f},{base_y:.1f} V{y0 + r:.1f} Q{x0:.1f},{y0:.1f} {x0 + r:.1f},{y0:.1f} "
         f"H{x1 - r:.1f} Q{x1:.1f},{y0:.1f} {x1:.1f},{y0 + r:.1f} V{base_y:.1f} Z")
    return f'<path d="{d}" fill="var(--{cls})"><title>{tip}</title></path>'


def _chart(result: AuditResult) -> str:
    rows = result.rows
    width, height, left, bottom, top = 760, 260, 48, 40, 22
    plot_w, plot_h = width - left - 16, height - bottom - top
    band = plot_w / max(len(rows), 1)
    bar = 24
    base_y = top + plot_h
    y = lambda v: top + plot_h * (1 - v)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Certified coverage by error budget">', '<g class="grid">']
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{left}" x2="{width - 16}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{y(t) + 4:.1f}" text-anchor="end">{int(t * 100)}%</text>')
    parts.append("</g>")
    for i, row in enumerate(rows):
        cx = left + band * (i + 0.5)
        for offset, value, cls, label in ((-bar / 2 - 1, row.coverage, "series-sundr", "Sundr"),
                                          (bar / 2 + 1, row.baseline_coverage, "series-base", "One question")):
            parts.append(_column(cx + offset, value, base_y, plot_h, bar, cls, f"{label}: {value:.0%} at a {row.epsilon:.1%} budget"))
            parts.append(f'<text class="val" x="{cx + offset:.1f}" y="{y(value) - 7:.1f}" text-anchor="middle">{value:.0%}</text>')
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{height - 14}" text-anchor="middle">{row.epsilon:.1%} budget</text>')
    parts.append("</svg>")
    return "".join(parts)


def _env() -> Environment:
    env = Environment(autoescape=select_autoescape(default=True))
    money = lambda v: "—" if v is None else f"${v:,.0f}"
    env.filters.update(
        pct=lambda v: f"{v * 100:.1f}".rstrip("0").rstrip(".") + "%",
        pct0=lambda v: f"{v:.0%}",
        pct1=lambda v: f"{v:.1%}",
        pct2=lambda v: f"{v:.2%}",
        usd=money,
        usd4=lambda v: "—" if v is None else f"${v:,.4f}" if v < 1 else f"${v:,.2f}",
        thousands=lambda v: f"{v:,}",
    )
    return env


def _options(result: AuditResult, preferred: float = 0.01) -> list[dict]:
    """Every way to take over the task, decision models first, with coverage per budget and net at `preferred`."""
    best = result.best_row(preferred)
    options = [{
        "label": "Decision models (" + ", ".join(b.split(" ")[0] for b in result.backends) + ")",
        "note": "Sundr's split questions", "agreement": result.agreement_all, "switch_bound": None,
        "coverage": {row.epsilon: row.coverage for row in result.rows}, "cost": result.decision_usd_per_call,
        "net": next((row.monthly_net_usd for row in result.rows if abs(row.epsilon - preferred) < 1e-9), None),
        "best": best,
    }]
    for c in result.candidates:
        row = c.best_row(preferred)
        options.append({
            "label": c.name, "note": "gated by decision models" if any(r.strategy == "gated" and r.certificate for r in c.rows) else None,
            "agreement": c.agreement, "switch_bound": c.switch_all_bound,
            "coverage": {r.epsilon: r.coverage for r in c.rows}, "cost": c.usd_per_call,
            "net": next((r.monthly_net_usd for r in c.rows if abs(r.epsilon - preferred) < 1e-9), None),
            "best": row,
        })
    return options


def _top(options: list[dict]) -> dict | None:
    """The option to lead with: most net savings at its best budget, else most coverage."""
    ranked = [o for o in options if o["best"] is not None and o["best"].coverage > 0]
    if not ranked:
        return None
    top = max(ranked, key=lambda o: (o["best"].monthly_net_usd or 0, o["best"].coverage))
    label = "Sundr's decision models" if top is options[0] else top["label"]
    return {"label": label, "epsilon": top["best"].epsilon, "coverage": top["best"].coverage,
            "net": top["best"].monthly_net_usd}


def render_report(result: AuditResult) -> str:
    template = _env().from_string(TEMPLATE)
    options = _options(result)
    budgets = [row.epsilon for row in result.rows if row.epsilon >= 0.01][:3]
    return template.render(r=result, best=result.best_row(), top=_top(options), options=options,
                           budgets=budgets, preferred=0.01, chart=_chart(result))


def write_report(result: AuditResult, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(render_report(result))
    summary = {
        "task": result.task.name,
        "n_records": result.n_records,
        "n_calibration": result.n_calibration,
        "agreement_all": result.agreement_all,
        "baseline_agreement_all": result.baseline_agreement_all,
        "decision_usd_per_call": result.decision_usd_per_call,
        "latency_p50_ms": result.latency_p50_ms,
        "questions_kept": [q.name for q in result.questions_kept],
        "rows": [
            {
                "epsilon": row.epsilon,
                "coverage": row.coverage,
                "baseline_coverage": row.baseline_coverage,
                "certificate": row.certificate.to_dict() if row.certificate else None,
                "monthly_net_usd": row.monthly_net_usd,
            }
            for row in result.rows
        ],
    }
    path.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    return path
