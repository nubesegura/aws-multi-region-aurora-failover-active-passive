"""Layered view of the DR demo (docs/architecture-layers.drawio): horizontal tier swimlanes on one column grid.

Source of truth: templates/*.yaml, scripts/deploy.sh and .github/workflows/*.yml.
Run from anywhere:
    python scripts/diagrams/architecture_layers.py                   # writes docs/architecture-layers.drawio
    python scripts/diagrams/architecture_layers.py --png <out.png>   # also renders a PNG with headless Chrome
Render to a temporary path, look at the image, then move it over docs/architecture-layers.png.
"""
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drawio_lib import Diagram, export_png  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "architecture-layers.drawio"

# ---- grid ----------------------------------------------------------------------------------------
LANE_X, STRIP = 20, 60
COL0, COLW, BOXW = 110, 290, 250
LANE_H, LANE_W = 240, 2400
TOP = 110
BOX_DY, BOX_H = 70, 120


def X(i):  # left edge of the box in column i
    return COL0 + COLW * i


def CX(i):  # centre of column i
    return X(i) + BOXW // 2


def LT(k):  # top of lane k
    return TOP + LANE_H * k


def BT(k):  # top of the boxes of lane k
    return LT(k) + BOX_DY


W, H = 2440, 1900
d = Diagram("DR demo layers", W, H)

TIERS = [
    ("Client tier", "#f5f5f5"),
    ("Edge and identity tier", "#e8f4f8"),
    ("API tier", "#f3e9f7"),
    ("Compute tier", "#e8f5e9"),
    ("Data tier", "#fff0e6"),
    ("Platform tier", "#fce4ec"),
]
for k, (name, fill) in enumerate(TIERS):
    d.cells.append(
        f'<mxCell id="lane{k}" value="{escape(name, quote=True)}" '
        f'style="swimlane;horizontal=0;startSize={STRIP};fillColor={fill};html=1;fontStyle=1;fontSize=15;'
        f'strokeColor=#232F3E;" vertex="1" parent="1">'
        f'<mxGeometry x="{LANE_X}" y="{LT(k)}" width="{LANE_W}" height="{LANE_H}" as="geometry"/></mxCell>'
    )

d.text("title", 20, 18, 2400, 80,
       "<b>AWS DR demo - layered view (prod, active-standby: us-east-2 primary at creation, us-west-2 secondary, "
       "us-east-1 global stacks)</b><br>"
       "Request path from the browser to the data, and the platform around it. "
       "Solid arrow: request or data path. Dashed arrow: DNS, standby path or asynchronous replication.", fs=16)

STYLES = {
    "plain": ("#ffffff", "#666666", False),
    "edge": ("#b1ddf0", "#10739e", False),
    "ident": ("#f8d7da", "#b85450", False),
    "api": ("#e1d5e7", "#9673a6", False),
    "compute": ("#d5e8d4", "#82b366", False),
    "data": ("#ffe6cc", "#d79b00", False),
    "platform": ("#f8cecc", "#b85450", False),
    "external": ("#fafafa", "#7f7f7f", True),
}


def box(cid, col, lane, kind, title, sub="", span=1):
    fill, stroke, dashed = STYLES[kind]
    label = f"<b>{title}</b>" + (f"<br>{sub}" if sub else "")
    d.box(cid, X(col), BT(lane), BOXW + COLW * (span - 1), BOX_H, label, fill=fill, stroke=stroke, dashed=dashed,
          sw=1, valign="middle", align="center", fs=13, bold=False, arc=12)


# client (one browser, two sites)
box("browser", 1, 0, "plain", "Presenter browser",
    "online banking app (bank-operator) and<br>DR monitoring dashboard (dashboard-viewer)", span=2)
# edge and identity
box("s3b", 0, 1, "edge", "S3 banking bucket", "private, versioned, read<br>only through OAC")
box("cfb", 1, 1, "edge", "CloudFront (banking)", "*.cloudfront.net, HTTPS,<br>CSP and HSTS headers")
box("cfm", 2, 1, "edge", "CloudFront (monitor)", "own address, same OAC<br>and headers policy")
box("s3m", 3, 1, "edge", "S3 monitor bucket", "private, versioned, read<br>only through OAC")
box("cogP", 4, 1, "ident", "Cognito user pool (us-east-2)", "primary, Essentials, KMS<br>multi-Region key, 2 clients")
box("cogR", 5, 1, "ident", "Cognito replica (us-west-2)", "same pool and client IDs,<br>attachment INACTIVE at rest")
box("r53", 6, 1, "edge", "Route 53 (us-east-1)", "weighted alias, one API<br>domain: use2 255, usw2 0")
# api
box("apiE", 6, 2, "api", "API Gateway REST (us-east-2)", "regional custom domain TLS 1.2,<br>Cognito authorizer, 20/40 rps")
box("apiW", 7, 2, "api", "API Gateway REST (us-west-2)", "same template, authorizer<br>on the replica pool")
d.text("apinote", X(0), BT(2) + 10, COLW * 5 + BOXW - 40, 110,
       "Routes: POST /transactions, GET /transactions, /stats, /account, /consistency need Authorization: Bearer ID token; "
       "GET /health and the CORS preflight need none. No API key, no usage plan, no WAF (accepted risk).", fs=14)
# compute
box("fnE", 6, 3, "compute", "6 Lambda functions (us-east-2)", "python3.14 arm64, VPC 10.40.0.0/24<br>private subnets, no NAT")
box("fnW", 7, 3, "compute", "6 Lambda functions (us-west-2)", "same code, VPC 10.41.0.0/24<br>private subnets, no NAT")
d.text("fnnote", X(0), BT(3) + 10, COLW * 5 + BOXW - 40, 110,
       "create-transaction is the only writer (balance and ledger under a row lock); list-transactions, get-stats, "
       "get-account, get-consistency and get-health only read. One IAM role per function. The drill fences a region "
       "by setting reserved concurrency 0 on its six functions.", fs=14)
# data
box("dbE", 6, 4, "data", "Aurora PostgreSQL 16.15", "us-east-2, writer at creation,<br>db.r6g.large, global cluster")
box("dbW", 7, 4, "data", "Aurora PostgreSQL 16.15", "us-west-2, reader at creation,<br>db.r6g.large")
box("kmsC", 4, 4, "data", "KMS multi-Region key", "us-east-2 primary, encrypts<br>Cognito credentials")
box("kmsR", 5, 4, "data", "KMS replica key", "us-west-2, used by the<br>Cognito replica")
# platform
box("gh", 0, 5, "platform", "GitHub Actions", "deploy, destroy, simulate-<br>outage, failover (manual)")
box("exec", 1, 5, "platform", "CloudFormation execution role", "bootstrap stack us-east-1,<br>plus artifact buckets")
box("base", 2, 5, "external", "Account baseline", "other repository: GitHub OIDC<br>provider, API Gateway log role")
box("ssm", 3, 5, "platform", "SSM parameters", "pool ID, pool ARN and<br>client IDs per region")
box("cw", 4, 5, "platform", "CloudWatch", "logs 30 days (KMS), 8 alarms<br>per region")
box("sns", 5, 5, "platform", "SNS alert topic", "per region, KMS encrypted,<br>receives the alarms")
box("kmsD", 6, 5, "platform", "KMS data key per region", "Aurora, logs, SNS,<br>master secret")
box("sm", 7, 5, "platform", "Secrets Manager", "Aurora master secret<br>(us-east-2), schema only")

# ---- flows ----------------------------------------------------------------------------------------
V, N = (0.5, 1), (0.5, 0)
BW2 = BOXW + COLW  # width of the browser box
yDNS = BT(0) + 0.3 * BOX_H
yCOG = BT(0) + 0.7 * BOX_H
d.edge("browser", "cfb", ((CX(1) - X(1)) / BW2, 1), N, num=1, lp=0.45)
d.edge("cfb", "s3b", (0, 0.5), (1, 0.5), num=2)
d.edge("browser", "cfm", ((CX(2) - X(1)) / BW2, 1), N, num=3, lp=0.45)
d.edge("cfm", "s3m", (1, 0.5), (0, 0.5), num=4)
d.edge("browser", "cogP", (1, 0.7), N, [(CX(4), yCOG)], num=5, lp=-0.3)
d.edge("browser", "r53", (1, 0.3), N, [(CX(6), yDNS)], num=6, dashed=True, lp=-0.2)
d.edge("r53", "apiE", V, N, num=7, lp=0.45)
d.edge("r53", "apiW", (1, 0.5), N, [(CX(7), BT(1) + BOX_H / 2)], num=8, dashed=True, lp=-0.4)
d.edge("apiE", "fnE", V, N, num=9, lp=0.45)
d.edge("apiW", "fnW", V, N)
d.edge("fnE", "dbE", V, N, num=10, lp=0.45)
d.edge("fnW", "dbW", V, N)
d.edge("dbE", "dbW", (1, 0.5), (0, 0.5), num=11, dashed=True)
d.edge("cogP", "cogR", (1, 0.5), (0, 0.5), num=12, dashed=True)
d.edge("gh", "exec", (1, 0.5), (0, 0.5), num=13)

# ---- legend and notes ------------------------------------------------------------------------------
LEG_Y = LT(6) + 40
d.box("legend", 20, LEG_Y, 2400, 270, "", fill="#ffffff", stroke="#232F3E", sw=1, arc=0)
d.text("legL", 40, LEG_Y + 15, 1160, 250,
       "<b>Flow legend</b><br>"
       "[1] [2] The online banking app loads from its own CloudFront distribution, which reads a private S3 bucket (OAC).<br>"
       "[3] [4] The read-only DR monitoring dashboard loads from a second distribution and its own private bucket.<br>"
       "[5] The browser signs in directly to Cognito (USER_PASSWORD_AUTH) and keeps a 15-minute ID token; after a failover it "
       "signs in again against the replica in us-west-2.<br>"
       "[6] The browser resolves the single API domain in Route 53 (dashed: DNS, about 60 s TTL) and sends the ID token as Bearer.<br>"
       "[7] Weight 255 sends every call to the active region. [8] Weight 0 keeps the standby region idle (dashed).<br>"
       "[9] API Gateway validates the token with its regional Cognito authorizer and proxies to the function of the route, which re-checks aud and token_use.<br>"
       "[10] The functions reach Aurora on 5432 over TLS with IAM database authentication (app_writer or app_reader).<br>"
       "[11] Aurora Global Database replicates asynchronously to us-west-2 (dashed). "
       "[12] Cognito multi-Region replication copies users and credentials to the replica (dashed).<br>"
       "[13] GitHub Actions assumes the organization role through OIDC and deploys every stack through the execution role.",
       fs=14)
d.text("legR", 1230, LEG_Y + 15, 1170, 250,
       "<b>Notes</b><br>"
       "Failover (workflow failover.yml): fence the old region (reserved concurrency 0), activate the replica attachment, "
       "planned switchover of the global cluster, then move the Route 53 weights; failback reverses the order. "
       "These control-plane calls are not drawn.<br>"
       "The dns stack and the global bootstrap stay in us-east-1 so the weight switch works while us-east-2 is down.<br>"
       "No internet gateway, NAT or VPC endpoints: the functions reach only the database; security groups allow 5432 from the "
       "function group only.<br>"
       "Not drawn: one IAM role per function, CloudWatch logs and alarms of every component, KMS encryption at rest.<br>"
       "The components view (architecture.drawio) shows the same system as AWS resources, account, regions and VPCs.",
       fs=14)

xml = d.xml()
OUT.write_text(xml, encoding="utf-8", newline="\n")
print("written", OUT, W, H)
if "--png" in sys.argv:
    export_png(xml, W, H, sys.argv[sys.argv.index("--png") + 1])
