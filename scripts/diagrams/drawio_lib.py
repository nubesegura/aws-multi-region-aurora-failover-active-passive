"""Minimal draw.io (mxGraph) XML builder for architecture diagrams.

Shared by the diagram builders in this folder (architecture.py, architecture_layers.py).
Everything is placed with ABSOLUTE coordinates and every arrow is routed by hand, so that the
layout is deterministic: draw containers first, then icons and boxes, then arrows.

    from drawio_lib import Diagram, viewer_html
    d = Diagram("name", width, height)
    d.box(id, x, y, w, h, label, ...)         # container, group or text box
    d.icon(id, cx, cy, "lambda", "compute", "label", lab="below|above|left|right")   # AWS icon (78 px)
    d.edge(src_id, dst_id, exit_xy, entry_xy, waypoints, num=1, dashed=False, lp=0.0)
    d.text(id, x, y, w, h, html)              # legend and notes
    xml = d.xml()                              # raises if the XML does not parse
    html = viewer_html(xml, width, height)     # page to open in a browser and screenshot to PNG

Edge points: exit_xy and entry_xy are fractions (0..1) of the source and target bounds; waypoints are
absolute (x, y) corners, so that every segment is horizontal or vertical. `lp` slides the number
label along the edge (-1..1, 0 = middle) so it does not sit on a container border.
Icon names are mxgraph.aws4 resource icons (lambda, api_gateway, dynamodb, s3, cloudfront, route_53,
cognito, sns, eventbridge, cloudwatch_2, cloudformation, certificate_manager_3, users, ...);
category colours: compute, storage, db, net, integ, mgmt, sec, user.
"""
import json
import xml.dom.minidom
from html import escape

NAVY = "#232F3E"
ICON = 78

CAT = {
    "compute": "#ED7100",
    "storage": "#7AA116",
    "db": "#C925D1",
    "net": "#8C4FFF",
    "integ": "#E7157B",
    "sec": "#DD344C",
    "mgmt": "#E7157B",
    "user": "#232F3E",
}


class Diagram:
    def __init__(self, name, width, height):
        self.name, self.w, self.h = name, width, height
        self.cells = []
        self._n = 1

    def _id(self, prefix="c"):
        self._n += 1
        return f"{prefix}{self._n}"

    def _add(self, cid, value, style, x, y, w, h, vertex=True):
        self.cells.append(
            f'<mxCell id="{cid}" value="{escape(value, quote=True)}" style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )

    def box(self, cid, x, y, w, h, label="", fill="none", stroke=NAVY, dashed=False, sw=2,
            valign="top", align="left", fs=14, bold=True, fcolor=NAVY, arc=2, extra=""):
        st = (f"rounded=1;arcSize={arc};whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
              f"strokeWidth={sw};dashed={1 if dashed else 0};verticalAlign={valign};align={align};"
              f"fontSize={fs};fontStyle={1 if bold else 0};fontColor={fcolor};spacingLeft=14;spacingRight=14;"
              f"spacingTop=8;spacingBottom=8;{extra}")
        self._add(cid, label, st, x, y, w, h)

    def text(self, cid, x, y, w, h, label, fs=13, align="left", fcolor=NAVY, bold=False):
        st = (f"text;html=1;whiteSpace=wrap;overflow=hidden;verticalAlign=top;align={align};"
              f"fontSize={fs};fontColor={fcolor};fontStyle={1 if bold else 0};spacing=0;")
        self._add(cid, label, st, x, y, w, h)

    def icon(self, cid, cx, cy, res, cat, label, lab="below"):
        pos = {
            "below": "verticalLabelPosition=bottom;verticalAlign=top;align=center;labelPosition=center;",
            "above": "verticalLabelPosition=top;verticalAlign=bottom;align=center;labelPosition=center;",
            "right": "labelPosition=right;verticalLabelPosition=middle;align=left;verticalAlign=middle;spacingLeft=10;",
            "left": "labelPosition=left;verticalLabelPosition=middle;align=right;verticalAlign=middle;spacingRight=10;",
        }[lab]
        st = ("sketch=0;outlineConnect=0;html=1;whiteSpace=nowrap;aspect=fixed;fontSize=13;"
              f"fontColor={NAVY};fillColor={CAT[cat]};strokeColor=#ffffff;dashed=0;"
              f"shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res};{pos}")
        self._add(cid, label, st, cx - ICON // 2, cy - ICON // 2, ICON, ICON)

    def edge(self, src, dst, exit_, entry, pts=(), num=None, dashed=False, color=NAVY, lp=0.0):
        eid = self._id("e")
        st = ("edgeStyle=none;rounded=0;html=1;endArrow=block;endFill=1;strokeWidth=2.5;"
              f"strokeColor={color};dashed={1 if dashed else 0};"
              f"exitX={exit_[0]};exitY={exit_[1]};exitDx=0;exitDy=0;"
              f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;"
              f"labelBackgroundColor=#ffffff;labelBorderColor={NAVY};fontStyle=1;fontSize=14;fontColor={NAVY};")
        geo = '<mxGeometry relative="1" as="geometry">'
        if lp:
            geo += f'<mxPoint x="0" y="0" as="offset"/>'
        if pts:
            geo += '<Array as="points">' + "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in pts) + "</Array>"
        geo += "</mxGeometry>"
        if lp:
            geo = geo.replace('<mxGeometry relative="1" as="geometry">', f'<mxGeometry x="{lp}" relative="1" as="geometry">')
        self.cells.append(
            f'<mxCell id="{eid}" value="{"" if num is None else num}" style="{st}" edge="1" parent="1" '
            f'source="{src}" target="{dst}">{geo}</mxCell>'
        )

    def xml(self):
        body = "".join(self.cells)
        raw = (
            f'<mxfile host="app.diagrams.net"><diagram id="{self.name}" name="{self.name}">'
            f'<mxGraphModel dx="{self.w}" dy="{self.h}" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
            f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{self.w}" pageHeight="{self.h}" '
            f'background="#ffffff" math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            f"{body}</root></mxGraphModel></diagram></mxfile>"
        )
        xml.dom.minidom.parseString(raw)  # verify it parses
        return raw


CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
)


def export_png(xml_text, w, h, out_png):
    """Render the diagram with the diagrams.net viewer in headless Chrome and write out_png.

    The page loads viewer-static.min.js from viewer.diagrams.net; the diagram data stays on this machine.
    Write to a temporary path, look at the image, then move it over the versioned file.
    """
    import os
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    chrome = next((c for c in CHROME_CANDIDATES if os.path.exists(c) or shutil.which(c)), None)
    if chrome is None:
        raise SystemExit("Chrome not found: export the PNG by hand from the .drawio file")
    page = Path(tempfile.mkdtemp()) / "diagram.html"
    page.write_text(viewer_html(xml_text, w, h), encoding="utf-8")
    out = Path(out_png).resolve()
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h + 60}",
         "--force-device-scale-factor=1.5", "--virtual-time-budget=15000", f"--screenshot={out}", page.as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    print("png written", out)


def viewer_html(xml_text, w, h):
    cfg = json.dumps({"highlight": "#0000ff", "nav": False, "resize": True, "toolbar": "", "xml": xml_text})
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;background:#fff}</style></head><body>"
        f"<div class='mxgraph' style='width:{w}px;height:{h}px' data-mxgraph='{escape(cfg, quote=True)}'></div>"
        "<script src='https://viewer.diagrams.net/js/viewer-static.min.js'></script></body></html>"
    )
