"""DR flow diagrams: what changes in which AWS resource during the three demo workflows.

docs/dr-outage-flow.drawio    demo-1-outage    (fence us-east-2; same as simulate-outage fence)
docs/dr-failover-flow.drawio  demo-2-failover  (us-east-2 -> us-west-2; same as failover to-secondary)
docs/dr-failback-flow.drawio  demo-3-failback  (us-west-2 -> us-east-2; same as failover to-primary)

Rows are the steps in time order; columns are where the change happens (GitHub Actions, us-east-2, Route 53, us-west-2).
Each changed resource shows its icon and "before -> after".
Source of truth: .github/workflows/demo-*.yml, failover.yml, scripts/dr/*.sh and docs/dr-test-report.md (measured values).
Run from anywhere:
    python scripts/diagrams/dr_flows.py          # writes the three .drawio files in docs/
    python scripts/diagrams/dr_flows.py --png    # also renders the three .png files
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drawio_lib import NAVY, Diagram, export_png  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs"
RED, GREEN, GREY, AMBER = "#C7131F", "#1D8102", "#5F6B7A", "#B7791F"

W = 1900
STEP_X, STEP_W = 40, 470
COLS = {  # name: (x, w, title, fill)
    "use2": (540, 420, "us-east-2 (primary at rest)", "#F2F8FD"),
    "glob": (990, 380, "Global (Route 53 public DNS)", "#F7F2FD"),
    "usw2": (1400, 460, "us-west-2 (secondary at rest)", "#F2FBF2"),
}
TOP = 150
ROW_H = 150
HDR = 60


def chg(before, after, color_after):
    return (f"<span style='color:{GREY}'>{before}</span> &#8594; "
            f"<b><span style='color:{color_after}'>{after}</span></b>")


def same(text):
    return f"<span style='color:{GREY}'>{text} (unchanged)</span>"


def build(name, title, subtitle, steps, footer):
    h = TOP + HDR + len(steps) * ROW_H + 160
    d = Diagram(name, W, h)
    d.text("title", 40, 30, W - 80, 40, f"<b>{title}</b>", fs=28)
    d.text("sub", 40, 80, W - 80, 50, subtitle, fs=16, fcolor=GREY)

    body_h = HDR + len(steps) * ROW_H + 20
    d.box("lane_steps", STEP_X - 10, TOP, STEP_W + 20, body_h, "GitHub Actions workflow steps",
          fill="#FAFAFA", stroke=NAVY, fs=16)
    for key, (x, w, t, fill) in COLS.items():
        d.box("lane_" + key, x, TOP, w, body_h, t, fill=fill, stroke=NAVY, fs=16, dashed=(key == "glob"))

    prev = None
    for i, s in enumerate(steps):
        cy = TOP + HDR + i * ROW_H + ROW_H // 2 - 10
        sid = f"s{i}"
        kind = s.get("kind", "change")
        fill = {"change": "#FFFFFF", "check": "#EEF6EE", "pre": "#FFF6E5", "effect": "#FDECEC", "same": "#F3F4F6"}[kind]
        stroke = {"change": NAVY, "check": GREEN, "pre": AMBER, "effect": RED, "same": GREY}[kind]
        head = f"{s['n']}. {s['title']}" if "n" in s else s["title"]
        if s.get("cells"):  # row band across the region lanes so the eye links the step with its changes
            x1 = COLS["usw2"][0] + COLS["usw2"][1] - 8
            d.box(f"band{i}", STEP_X + STEP_W - 2, cy - 55, x1 - STEP_X - STEP_W + 2, 110, "",
                  fill="#FFFFFF", stroke="#9AA5B1", dashed=True, sw=1.5, arc=6, extra="opacity=85;")
        d.box(sid, STEP_X, cy - 50, STEP_W, 100,
              f"<b>{head}</b><br><span style='font-weight:normal'>{s['detail']}</span>",
              fill=fill, stroke=stroke, fs=15, bold=False, valign="middle", arc=8)
        if prev is not None:
            d.edge(prev, sid, (0.5, 1), (0.5, 0))
        prev = sid
        for col, (res, cat, label) in s.get("cells", {}).items():
            d.icon(f"i{i}{col}", COLS[col][0] + 60, cy, res, cat, label, lab="right")

    d.text("foot", 40, TOP + body_h + 20, W - 80, 200, footer, fs=15)
    xml = d.xml().replace("aspect=fixed;fontSize=13;", "aspect=fixed;fontSize=17;")
    out = DOCS / f"{name}.drawio"
    out.write_text(xml, encoding="utf-8", newline="\n")
    print("written", out)
    if "--png" in sys.argv:
        export_png(xml, W, h, str(DOCS / f"{name}.png"))


FENCED = "fenced (concurrency 0)"
SERVING = "serving"

failover = [
    dict(n=0, kind="pre", title="demo-1-outage (fence us-east-2)",
         detail="Previous workflow, see the outage diagram. The database is not touched. Measured: 33 s.",
         cells={"use2": ("lambda", "compute", "6 Lambda functions<br>" + chg(SERVING, FENCED, RED))}),
    dict(n=1, kind="check", title="Pre-checks (read only)",
         detail="Stops unless us-east-2 is fenced and us-west-2 is not."),
    dict(n=2, title="Activate the Cognito replica",
         detail="CloudFormation update of the cognito stack in us-west-2. Users can sign in there.",
         cells={"usw2": ("cognito", "sec", "Cognito replica attachment<br>" + chg("INACTIVE", "ACTIVE", GREEN))}),
    dict(n=3, title="Aurora planned switchover",
         detail="Waits for replication to catch up, then swaps roles. No data loss (RPO 0). About 35 s.",
         cells={"use2": ("aurora", "db", "Aurora cluster<br>" + chg("writer", "reader", RED)),
                "usw2": ("aurora", "db", "Aurora cluster<br>" + chg("reader", "writer", GREEN))}),
    dict(n=4, title="Switch the Route 53 weights",
         detail="Update of the dns stack (us-east-1). Users now reach the API in us-west-2.",
         cells={"glob": ("route_53", "net", "Weighted records<br>use2 / usw2<br>" + chg("255 / 0", "0 / 255", GREEN))}),
    dict(n=5, kind="check", title="Validate on the public API name",
         detail="GET /health?deep=true must answer from us-west-2 with database role writer."),
    dict(n=6, kind="check", title="Ledger consistency check",
         detail="Replays the ledger: no movement lost or duplicated. Needs a Cognito token."),
]

failback = [
    dict(n=1, title="Unfence us-east-2",
         detail="Removes the reserved concurrency. Functions answer again, but the database there is still a reader.",
         cells={"use2": ("lambda", "compute", "6 Lambda functions<br>" + chg(FENCED, SERVING, GREEN))}),
    dict(n=2, title="Aurora planned switchover back",
         detail="Waits for replication to catch up, then swaps roles again. No data loss. About 35 s.",
         cells={"use2": ("aurora", "db", "Aurora cluster<br>" + chg("reader", "writer", GREEN)),
                "usw2": ("aurora", "db", "Aurora cluster<br>" + chg("writer", "reader", RED))}),
    dict(n=3, title="Deactivate the Cognito replica",
         detail="CloudFormation update of the cognito stack in us-west-2. Sign-in goes back to the primary pool.",
         cells={"usw2": ("cognito", "sec", "Cognito replica attachment<br>" + chg("ACTIVE", "INACTIVE", GREY))}),
    dict(n=4, title="Switch the Route 53 weights back",
         detail="Update of the dns stack (us-east-1). Users reach the API in us-east-2 again.",
         cells={"glob": ("route_53", "net", "Weighted records<br>use2 / usw2<br>" + chg("0 / 255", "255 / 0", GREEN))}),
    dict(n=5, kind="check", title="Validate on the public API name",
         detail="GET /health?deep=true must answer from us-east-2 with database role writer."),
    dict(n=6, kind="check", title="Ledger consistency check",
         detail="Replays the ledger: no movement lost or duplicated. Needs a Cognito token."),
]

outage = [
    dict(n=1, kind="check", title="Validate branch and configuration",
         detail="Runs only from main. No inputs: the region is fixed to the primary region of the prod environment."),
    dict(n=2, title="Fence us-east-2",
         detail="10-simulate-outage.sh sets reserved concurrency 0 on the six API functions. Measured: 13 s (run 33 s).",
         cells={"use2": ("lambda", "compute", "6 Lambda functions<br>" + chg(SERVING, FENCED, RED))}),
    dict(n=3, kind="check", title="Show the fence status",
         detail="Read only: the six functions of us-east-2 report reserved concurrency 0."),
    dict(kind="effect", title="What users see: the outage",
         detail="The public API name still points to us-east-2, so every request there is throttled.",
         cells={"use2": ("api_gateway", "net", "Regional API<br>" + chg("HTTP 200", "HTTP 429 / 500", RED)),
                "glob": ("route_53", "net", "Weighted records<br>use2 / usw2<br>" + same("255 / 0"))}),
    dict(kind="same", title="What does NOT change",
         detail="The database is not touched: no data is lost and the replication keeps running. Cognito stays as it is.",
         cells={"use2": ("aurora", "db", "Aurora cluster<br>" + same("writer")),
                "usw2": ("aurora", "db", "Aurora cluster<br>" + same("reader, replicating"))}),
]

LEGEND = ("<b>Legend.</b> White box: step that changes an AWS resource. Green box: read-only check. Amber box: step before the "
          "failover. Each icon shows the resource that changes in that step as <i>before &#8594; after</i>. "
          "Resources not shown do not change (API Gateway, the code of the functions, Cognito primary pool, CloudFront sites). "
          "The workflow stops at the first failing step and leaves the state as it is.")

build("dr-outage-flow",
      "Simulated outage: us-east-2 stops serving",
      "Workflow <b>demo-1-outage</b> (no inputs; same as <b>simulate-outage</b>, action fence, region us-east-2). "
      "It simulates the loss of the region by throttling its functions; it does not break the database.",
      outage,
      "<b>Legend.</b> White box: step that changes an AWS resource. Green box: read-only check. Red box: effect on the users. "
      "Grey box: what stays the same. Each icon shows a resource as <i>before &#8594; after</i>."
      "<br><br><b>End state:</b> us-east-2 answers errors, us-west-2 is healthy but receives no traffic yet. "
      "Next: <b>demo-2-failover</b>. Undo: <b>demo-3-failback</b> lifts the fence first.")

build("dr-failover-flow",
      "Failover: us-east-2 &#8594; us-west-2 (planned switchover)",
      "Workflow <b>demo-2-failover</b> (no inputs; same as <b>failover</b>, direction to-secondary). Order matters: fence the old region, move the database, "
      "only then move the traffic. Measured in the drill of 2026-11-11: write RTO 197 s, RPO 0 (504 of 504 movements).",
      failover,
      LEGEND + "<br><br><b>End state:</b> us-west-2 serves reads and writes; us-east-2 is fenced and its database is a reader "
      "that keeps receiving the replication.")

build("dr-failback-flow",
      "Failback: us-west-2 &#8594; us-east-2 (return to the primary region)",
      "Workflow <b>demo-3-failback</b> (no inputs; same as <b>failover</b>, direction to-primary). The fence is lifted first, then the database roles, then the weights. "
      "Measured in the drill of 2026-11-11: write RTO 83 s, RPO 0.",
      failback,
      LEGEND + "<br><br><b>End state:</b> back to the resting state. us-east-2 serves reads and writes; us-west-2 is a reader "
      "with the Cognito replica INACTIVE.")
