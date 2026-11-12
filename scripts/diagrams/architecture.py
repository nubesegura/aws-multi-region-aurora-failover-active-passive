"""AWS components diagram of the DR demo (docs/architecture.drawio).

Source of truth: templates/*.yaml, scripts/deploy.sh and .github/workflows/*.yml.
Run from anywhere:
    python scripts/diagrams/architecture.py                      # writes docs/architecture.drawio
    python scripts/diagrams/architecture.py --png <out.png>      # also renders a PNG with headless Chrome
Render to a temporary path, look at the image, then move it over docs/architecture.png.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drawio_lib import NAVY, Diagram, export_png  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "architecture.drawio"

W, H = 3000, 2330
d = Diagram("DR demo AWS components", W, H)

d.box("title", 40, 20, 2920, 60,
      "AWS DR demo - AWS components (prod, one account, active-standby: us-east-2 primary at creation, us-west-2 secondary)",
      fill=NAVY, stroke=NAVY, fcolor="#ffffff", fs=22, valign="middle")

# ---- grid ----------------------------------------------------------------------------------------
ACC_Y0, ACC_Y1 = 100, 1880
REG_Y0, REG_Y1 = 150, 1850
EDGE_Y0, EDGE_Y1 = 190, 560          # edge stack box (us-east-2 only)
MRR_Y = 590                          # corridor of the Cognito replication arrow
COG_Y0, COG_Y1, COG_CY = 620, 820, 680
R53W_Y = 850                         # corridor of the weight-0 arrow to the us-west-2 API
VPC_Y0, VPC_Y1 = 880, 1530
API_CY = 1030
GRP_Y0, GRP_Y1 = 930, 1420
FN_ROWS = (1020, 1170, 1320)
DB_CY = 1175
REPL_Y = 1475                        # corridor of the Aurora replication arrow
X_Y0, X_Y1, X_CY = 1570, 1810, 1670  # cross-cutting band
OFF = 1070                           # x offset between the two data regions

# ---- containers -----------------------------------------------------------------------------------
d.box("acct", 340, ACC_Y0, 2620, ACC_Y1 - ACC_Y0,
      "AWS account (prod) - three regions; Lambda in VPC private subnets, no internet gateway, no NAT, no VPC endpoints, no WAF",
      stroke=NAVY, sw=3, fs=15)
d.box("glob", 370, REG_Y0, 420, REG_Y1 - REG_Y0, "Region us-east-1 - global stacks", fill="#faf5ff", stroke="#8C4FFF", fs=15)
d.box("dnsbox", 400, 900, 360, 260, "dns stack (weights only)", fill="#ffffff", stroke="#8C4FFF", valign="bottom", fs=13)
d.box("bootg", 400, 1220, 360, 600, "bootstrap stack (global, by hand once)", fill="#ffffff", stroke="#DD344C", fs=13)

for tag, x0, name, role in (("E", 830, "us-east-2", "PRIMARY at creation"), ("W", 830 + OFF, "us-west-2", "SECONDARY at creation")):
    d.box("reg" + tag, x0, REG_Y0, 1030, REG_Y1 - REG_Y0, f"Region {name} - {role}", fill="#f4f9ff", stroke="#147EBA", fs=16)
    stack = "cognito stack: primary user pool" if tag == "E" else "cognito stack: replica configuration"
    d.box("cog" + tag, x0 + 30, COG_Y0, 970, COG_Y1 - COG_Y0, stack, fill="#ffffff", stroke="#DD344C", valign="bottom", fs=13)
    cidr = "10.40.0.0/24" if tag == "E" else "10.41.0.0/24"
    d.box("vpc" + tag, x0 + 250, VPC_Y0, 750, VPC_Y1 - VPC_Y0,
          f"network stack: VPC {cidr}, 2 private subnets (2 AZ), local routes only", fill="#ffffff", stroke="#147EBA", fs=13, dashed=True)
    d.box("grp" + tag, x0 + 280, GRP_Y0, 450, GRP_Y1 - GRP_Y0, "api stack: 6 Lambda arm64, 1 IAM role each",
          fill="#ffffff", stroke="#ED7100", fs=13)
    d.box("cross" + tag, x0 + 30, X_Y0, 970, X_Y1 - X_Y0, "Cross-cutting (not wired)", fill="#ffffff", stroke="#E7157B", fs=13)

d.box("edgeE", 860, EDGE_Y0, 970, EDGE_Y1 - EDGE_Y0, "edge stack: two static sites", fill="#ffffff", stroke="#8C4FFF", fs=13)
d.text("edgenote", 1400, 260, 400, 220,
       "One origin access control and one security-headers policy (CSP, HSTS) shared by both distributions."
       "<br><br>Default *.cloudfront.net certificates, HTTPS only. No WAF and no access logs (accepted).", fs=13)
d.text("wnote", 1930, 260, 900, 120,
       "No edge stack in us-west-2: both static sites are served by CloudFront from the private buckets in us-east-2.", fs=13)

# ---- external actors --------------------------------------------------------------------------------
BX, BY = 150, COG_CY
d.icon("browser", BX, BY, "users", "user", "Presenter browser<br>banking app, DR monitor")
d.box("gh", 50, 1290, 250, 150,
      "<b>GitHub Actions</b> (manual)<br>deploy, destroy,<br>simulate-outage, failover<br>OIDC, organization role",
      fill="#f1f5f9", stroke="#334155", valign="middle", align="center", bold=False, fs=13)
d.box("base", 50, 1520, 250, 170,
      "<b>Account baseline</b><br>(other repository)<br>GitHub OIDC provider,<br>API Gateway CloudWatch<br>role per region",
      fill="#fafafa", stroke="#7f7f7f", dashed=True, valign="middle", align="center", bold=False, fs=13)

# ---- us-east-1 -----------------------------------------------------------------------------------------
GX = 580
d.icon("r53", GX, API_CY, "route_53", "net", "Route 53 weighted alias<br>use2 255, usw2 0")
d.icon("exec", GX, 1340, "identity_and_access_management", "sec", "CloudFormation<br>execution role")
d.icon("artg", GX, 1530, "s3", "storage", "S3 artifact bucket<br>(global)")
d.icon("deploy", GX, 1700, "identity_and_access_management", "sec", "Deploy role<br>(created, not used)")

# ---- edge (us-east-2) ------------------------------------------------------------------------------------
d.icon("cfb", 960, 280, "cloudfront", "net", "CloudFront<br>online banking")
d.icon("s3b", 1180, 280, "s3", "storage", "S3 banking bucket<br>private, OAC", lab="right")
d.icon("cfm", 960, 450, "cloudfront", "net", "CloudFront<br>DR monitor")
d.icon("s3m", 1180, 450, "s3", "storage", "S3 monitor bucket<br>private, OAC", lab="right")

# ---- identity --------------------------------------------------------------------------------------------
xE, xW = 830, 830 + OFF
d.icon("pool", xE + 130, COG_CY, "cognito", "sec", "Cognito user pool<br>primary, Essentials")
d.icon("clients", xE + 370, COG_CY, "cognito", "sec", "2 app clients (no secret)<br>shared with the replica")
d.icon("kmsP", xE + 610, COG_CY, "key_management_service", "sec", "KMS multi-Region key<br>primary (Cognito)")
d.icon("ssmE", xE + 850, COG_CY, "systems_manager", "mgmt", "SSM parameters x4<br>pool ID, ARN, clients")
d.icon("replica", xW + 130, COG_CY, "cognito", "sec", "Cognito replica (MRR)<br>attachment INACTIVE")
d.icon("kmsR", xW + 370, COG_CY, "key_management_service", "sec", "KMS replica key<br>(Cognito)")
d.icon("ssmW", xW + 610, COG_CY, "systems_manager", "mgmt", "SSM parameters x4<br>same IDs, replica ARN")

# ---- API, compute and data per data region -------------------------------------------------------------------
FNS = ("create-transaction", "list-transactions", "get-stats", "get-health", "get-account", "get-consistency")
for tag, x0 in (("E", xE), ("W", xW)):
    d.icon("api" + tag, x0 + 130, API_CY, "api_gateway", "integ", "API Gateway REST<br>Cognito authorizer")
    d.icon("acm" + tag, x0 + 130, 1290, "certificate_manager_3", "sec", "ACM certificate<br>custom domain TLS 1.2")
    for i, nm in enumerate(FNS):
        d.icon(f"f{tag}{i}", x0 + 400 + (i % 2) * 210, FN_ROWS[i // 2], "lambda", "compute", nm)
    role = "writer at creation" if tag == "E" else "reader at creation"
    d.icon("db" + tag, x0 + 890, DB_CY, "aurora", "db", f"Aurora PostgreSQL 16.15<br>{role}", lab="above")
    items = [("cw", "cloudwatch_2", "mgmt", "CloudWatch Logs 30 d<br>8 alarms"),
             ("sns", "sns", "integ", "SNS alert topic<br>KMS encrypted"),
             ("kms", "key_management_service", "sec", "KMS data key<br>Aurora, logs, SNS"),
             ("art", "s3", "storage", "S3 artifact bucket<br>(data region)")]
    if tag == "E":
        items.append(("sm", "secrets_manager", "sec", "Secrets Manager<br>DB master secret"))
    for k, (cid, res, cat, label) in enumerate(items):
        d.icon(cid + tag, x0 + 120 + k * 200, X_CY, res, cat, label)

# ============================ arrows (numbers explained in the legend) ============================
# sites
d.edge("browser", "cfb", (1, 0.2), (0, 0.5), [(250, BY - 23), (250, 280)], num=1, lp=0.6)
d.edge("cfb", "s3b", (1, 0.5), (0, 0.5), num=2)
d.edge("browser", "cfm", (1, 0.35), (0, 0.5), [(290, BY - 12), (290, 450)], num=3, lp=0.6)
d.edge("cfm", "s3m", (1, 0.5), (0, 0.5), num=4)
# identity
d.edge("browser", "pool", (1, 0.5), (0, 0.5), num=5, lp=0.35)
# DNS and API
d.edge("browser", "r53", (1, 0.85), (0, 0.5), [(270, BY + 27), (270, API_CY)], num=6, dashed=True, lp=0.85)
d.edge("r53", "apiE", (1, 0.5), (0, 0.5), num=7, lp=-0.55)
d.edge("r53", "apiW", (0.5, 0), (0.5, 0), [(GX, R53W_Y), (xW + 130, R53W_Y)], num=8, dashed=True, lp=0.3)
for tag, x0 in (("E", xE), ("W", xW)):
    fy = (API_CY - GRP_Y0) / (GRP_Y1 - GRP_Y0)
    d.edge("api" + tag, "grp" + tag, (1, 0.5), (0, fy), num=9, lp=-0.45)
    gy = (DB_CY - GRP_Y0) / (GRP_Y1 - GRP_Y0)
    d.edge("grp" + tag, "db" + tag, (1, gy), (0, 0.5), num=10)
# replication
d.edge("dbE", "dbW", (0.5, 1), (0.5, 1), [(xE + 890, REPL_Y), (xW + 890, REPL_Y)], num=11, dashed=True, lp=0.0)
d.edge("pool", "replica", (0.5, 0), (0.5, 0), [(xE + 130, MRR_Y), (xW + 130, MRR_Y)], num=12, dashed=True, lp=0.0)
# delivery
d.edge("gh", "exec", (1, (1340 - 1290) / 150), (0, 0.5), num=13, lp=0.4)

# ---- legend and notes ------------------------------------------------------------------------------------
LY = 1920
d.box("legend", 40, LY, 2920, 370, "Legend and notes", fill="#f8fafc", stroke=NAVY, fs=16)
left = (
    "<b>Sites, sign-in and requests</b><br>"
    "[1] [2] The online banking app loads over HTTPS from its own CloudFront distribution, which reads a private S3 bucket "
    "through origin access control.<br>"
    "[3] [4] The read-only DR monitoring dashboard loads from a second distribution with its own private bucket.<br>"
    "[5] The browser signs in directly to the Cognito user pool (USER_PASSWORD_AUTH, no hosted UI) and keeps a 15-minute ID token (refresh 24 h); "
    "after a failover it signs in again against the replica in us-west-2 with the same pool and client IDs.<br>"
    "[6] The browser resolves the single API domain in Route 53 (dotted: DNS, about 60 s TTL) and sends Authorization: Bearer ID token.<br>"
    "[7] Weight 255: every call goes to the regional custom domain of the active region (us-east-2 at creation).<br>"
    "[8] Weight 0: the standby region receives nothing until the failover workflow moves the weights (dashed: standby).<br>"
    "[9] API Gateway validates the ID token with the native Cognito authorizer of its region and proxies to one of six functions, which re-check aud and token_use; "
    "only GET /health and the CORS preflight need no token. No API key and no usage plan.<br>"
    "[10] The functions reach Aurora on 5432 over TLS with IAM database authentication: create-transaction as app_writer, the other five "
    "as app_reader."
)
right = (
    "<b>Replication and delivery</b><br>"
    "[11] Aurora Global Database replicates asynchronously from us-east-2 to us-west-2 (dotted); a planned switchover swaps the roles.<br>"
    "[12] Cognito multi-Region replication copies users and credentials from us-east-2 to the replica (dotted), encrypted with the "
    "customer managed multi-Region key and its replica key. The regional configuration attachment is INACTIVE at rest and ACTIVE "
    "during a failover.<br>"
    "[13] GitHub Actions assumes the organization role through OIDC and deploys every stack through the CloudFormation execution role. "
    "The drill workflows fence functions (reserved concurrency 0), switch the database over, toggle the replica attachment and move the "
    "weights; these control-plane calls are not drawn.<br><br>"
    "<b>Notes</b><br>"
    "Stacks: bootstrap (us-east-1 by hand; artifact bucket per data region), edge, cognito, network, database, api in us-east-2 and us-west-2, "
    "dns in us-east-1. Aurora: one db.r6g.large instance per region; the global cluster and the master secret are in the us-east-2 stack. "
    "Alarms: API 4XX, 5XX, Lambda errors and throttles, certificate expiry, replication lag, database CPU and connections, all to the "
    "SNS alert topic. Security groups: functions reach only the database; the database accepts only the function group. "
    "Solid arrow: synchronous request. Dotted arrow: DNS, standby path or asynchronous replication. Dashed group: owned by another repository."
)
d.text("legL", 70, LY + 50, 1400, 310, left, fs=15)
d.text("legR", 1520, LY + 50, 1410, 310, right, fs=15)

xml = d.xml()
OUT.write_text(xml, encoding="utf-8", newline="\n")
print("written", OUT, W, H)
if "--png" in sys.argv:
    export_png(xml, W, H, sys.argv[sys.argv.index("--png") + 1])
