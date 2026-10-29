"""Generate docs/dashboard-mockup.svg: the Overview tab of the cuscope Azure Workbook, styled like the portal.

  python docs/make_mockup.py docs/dashboard-mockup.svg

The numbers are illustrative; the layout follows infra/workbook/build_workbook.py."""
import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1]
W, H = 1000, 1150
FONT = "Segoe UI, -apple-system, Helvetica, Arial, sans-serif"
TXT, MUTED, GRID, BORDER, BLUE = "#323130", "#605e5c", "#edebe9", "#c8c6c4", "#0f6cbd"
o = []


def t(x, y, s, size=11, fill=TXT, weight=400, anchor="start", extra=""):
    o.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
             f'text-anchor="{anchor}" {extra}>{escape(s)}</text>')


def r(x, y, w, h, fill="none", stroke="none", rx=0, sw=1, extra=""):
    o.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" '
             f'stroke="{stroke}" stroke-width="{sw}" {extra}/>')


def ln(x1, y1, x2, y2, stroke=GRID, sw=1, extra=""):
    o.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{sw}" {extra}/>')


def poly(points, stroke, sw=1.5):
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    o.append(f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="round"/>')


# ---- page chrome -------------------------------------------------------------------
r(0, 0, W, H, fill="#ffffff")
t(30, 34, "cuscope - Fabric CU attribution", 18, TXT, 600)
t(292, 34, "| Workbook", 18, MUTED)
t(30, 49, "Azure Workbook", 9.5, MUTED)
r(800, 18, 170, 22, fill="#fff4ce", rx=11)
t(885, 33, "Mock-up · illustrative data", 10.5, "#8a6d00", 600, "middle")

t(30, 84, "Fabric CU attribution", 14, TXT, 600)
t(30, 104, "Read from the Capacity Metrics model by the cuscope harvester (outside Fabric). CU figures are "
           "CU-seconds; percentages are of the capacity's base budget.", 10, TXT)

# parameter pills
r(30, 118, 196, 26, fill="#e5f1fb", stroke=BLUE, rx=13)
t(42, 135, "Capacity:", 10.5, TXT)
r(94, 123, 124, 16, fill="#ffffff", stroke="#8a8886", rx=2)
t(99, 135, "fabric-dev (F2)", 10, TXT)
t(209, 135, "⌄", 10, MUTED)
r(236, 118, 166, 26, fill="#ebf3fc", rx=13)
t(248, 135, "Time range:", 10.5, TXT)
t(310, 135, "Last 24 hours", 10.5, TXT, 600)
t(388, 135, "⌄", 10, MUTED)
r(412, 118, 210, 26, fill="#ebf3fc", rx=13)
t(424, 135, "Explain window (or brus… :", 10.5, TXT)
t(556, 135, "Last hour", 10.5, TXT, 600)
t(608, 135, "⌄", 10, MUTED)

# tabs
tabs = [("Overview", 30, 52), ("Explain a window", 100, 0), ("24h background", 208, 0),
        ("Root cause by run time", 306, 0), ("Daily changes", 441, 0), ("Details", 530, 0)]
for label, x, underline in tabs:
    t(x, 172, label, 11.5, TXT, 600 if underline else 400)
    if underline:
        ln(x, 180, x + underline, 180, BLUE, 2)

# ---- summary tiles -------------------------------------------------------------------
t(30, 210, "Summary for the time range", 11.5, TXT, 600)
tiles = [("Average utilisation (%)", "57.4", "#3abff0"), ("Peak utilisation (%)", "61.9", "#8661c5"),
         ("Timepoints over 100%", "0", "#4bd6c4"), ("Background, last 24 h (…", "55.6", "#e8825b"),
         ("Harvest errors (24 h)", "0", "#0f6cbd"), ("Minutes since last harvest", "2", "#a19f9d")]
for i, (title, value, color) in enumerate(tiles):
    x = 38 + i * 136
    r(x, 222, 126, 52, fill="#ffffff", stroke=BORDER)
    t(x + 6, 237, title, 9.5, TXT)
    r(x + 6, 246, 4, 22, fill=color)
    t(x + 15, 266, value, 22, TXT, 400)

# ---- utilisation chart ---------------------------------------------------------------
t(30, 306, "Smoothed utilisation: background and interactive (reconstructed) vs the app. The red line is the "
           "throttling threshold. Drag across the chart to set the explain window.", 9.5, TXT, 600)
X0, X1, Y0, Y1, VMAX = 70, 640, 530, 330, 120.0
def cx(h): return X0 + (X1 - X0) * h / 24.0
def cy(v): return Y0 - (Y0 - Y1) * v / VMAX
for v in range(0, 121, 20):
    ln(X0, cy(v), X1, cy(v))
    t(X0 - 8, cy(v) + 3, str(v), 8.5, MUTED, anchor="end")
ln(X0, Y0, X1, Y0, "#c8c6c4")

bg = [55.2, 55.6, 55.9, 56.1, 56.0, 55.8, 55.5, 55.7, 56.2, 56.8, 57.1, 57.0, 56.6,
      56.3, 56.0, 55.7, 55.9, 56.4, 56.9, 57.2, 56.8, 56.1, 55.6, 55.3, 55.1]
it = [0.4, 0.3, 0.2, 0.2, 0.2, 0.3, 0.6, 1.1, 2.4, 3.1, 2.8, 1.6, 2.2,
      3.4, 2.9, 1.8, 1.2, 0.9, 0.7, 0.5, 0.4, 0.3, 0.4, 0.6, 0.5]
noise = [0.05, -0.04, 0.03, 0.0, -0.06, 0.04, 0.02, -0.03, 0.05, -0.02, 0.03, -0.05, 0.01,
         0.04, -0.03, 0.02, -0.01, 0.05, -0.04, 0.03, 0.0, -0.02, 0.04, -0.05, 0.02]
recon = [b + i for b, i in zip(bg, it)]
app = [v + n for v, n in zip(recon, noise)]
hours = range(25)
poly([(cx(h), cy(100)) for h in (0, 24)], "#e81123", 1.5)
poly([(cx(h), cy(v)) for h, v in zip(hours, bg)], "#3a96dd")
poly([(cx(h), cy(v)) for h, v in zip(hours, it)], "#ff8c00")
poly([(cx(h), cy(v)) for h, v in zip(hours, recon)], "#a19f9d", 2.5)
poly([(cx(h), cy(v)) for h, v in zip(hours, app)], "#13a10e", 1.2)
# time brush handles, as the portal draws them
for x in (X0, X1):
    o.append(f'<path d="M{x - 5:.1f},{Y0 + 9} L{x:.1f},{Y0 + 1} L{x + 5:.1f},{Y0 + 9} Z" fill="#605e5c"/>')
for h, label in ((5, "Oct 28"), (11, "6 AM"), (17, "12 PM"), (23, "6 PM")):
    t(cx(h), Y0 + 20, label, 8.5, MUTED, anchor="middle")
legend = [("Background %", "#3a96dd"), ("Interactive %", "#ff8c00"), ("App utilisation %", "#13a10e"),
          ("Reconstructed total %", "#a19f9d"), ("Throttle limit %", "#e81123")]
lx = X0
for label, color in legend:
    r(lx, 563, 8, 8, fill=color)
    t(lx + 12, 571, label, 9.5, TXT)
    lx += 24 + len(label) * 5.6

# ---- top contributors ------------------------------------------------------------------
t(30, 610, "Top contributors in the explain window", 11.5, TXT, 600)
B0, B1, BV = 760, 630, 2000.0
def by(v): return B0 - (B0 - B1) * v / BV
for v, label in ((0, "0K"), (500, "0.5K"), (1000, "1K"), (1500, "1.5K"), (2000, "2K")):
    ln(X0, by(v), X1, by(v))
    t(X0 - 8, by(v) + 3, label, 8.5, MUTED, anchor="end")
bars = [("wh_loadgen (background)", 1700, "1.7", "K", "#6378f0"),
        ("lh_loadgen (background)", 1280, "1.28", "K", "#e3008c"),
        ("sm_loadgen (interactive)", 1110, "1.11", "K", "#2aa0a4"),
        ("pl_copy_foreach (background)", 962, "962", "", "#9373c0"),
        ("pl_wh_queries (background)", 85, "85", "", "#13a10e"),
        ("nb_spark_events (background)", 70, "70", "", "#3a96dd")]
step = (X1 - X0) / len(bars)
for i, (label, v, _, _, color) in enumerate(bars):
    c = X0 + step * (i + 0.5)
    r(c - 17, by(v), 34, B0 - by(v), fill=color)
    t(c + 8, B0 + 12, label, 8, MUTED, anchor="end", extra=f'transform="rotate(-25 {c + 8:.1f} {B0 + 12})"')
# metric strip under the chart (first page of four)
t(36, 832, "⌃", 9, MUTED); t(32, 845, "1/2", 8.5, MUTED); t(36, 857, "⌄", 9, MUTED)
for i, (label, _, value, unit, color) in enumerate(bars[:4]):
    x = 58 + i * 125
    r(x, 822, 3, 34, fill=color)
    t(x + 7, 831, label if len(label) < 26 else label[:24] + "…", 8, TXT)
    t(x + 7, 856, value, 24, TXT)
    if unit:
        t(x + 9 + sum(6 if ch == "." else 13 for ch in value), 856, unit, 11, TXT, 600)

# ---- harvester health grid -----------------------------------------------------------
t(30, 892, "Harvester health (last 24 h). Errors should be 0.", 11.5, TXT, 600)
r(30, 902, 940, 22, fill="#ffffff", stroke="#8a8886", rx=2)
t(40, 917, "⌕  Search", 10, MUTED)
cols = [("job", 38), ("capacity_id", 128), ("table_name", 300), ("runs", 420), ("errors", 480),
        ("empty", 548), ("rows", 610), ("last_run", 700)]
for name, x in cols:
    t(x, 946, name, 10, TXT, 600)
ln(30, 954, 970, 954, BORDER)
rows = [("background", "background", 48, 0, 0, "654,913", "10/28/2026, 6:32 PM"),
        ("interactive", "interactive", 288, 0, 0, "41,230", "10/28/2026, 6:55 PM"),
        ("timepoints", "timepoints", 24, 0, 0, "2,880", "10/28/2026, 6:25 PM"),
        ("enrich", "jobs", 96, 0, 12, "1,184", "10/28/2026, 6:52 PM"),
        ("enrich", "spark", 96, 0, 31, "212", "10/28/2026, 6:52 PM"),
        ("enrich", "warehouse", 96, 0, 0, "3,977", "10/28/2026, 6:52 PM"),
        ("dimensions", "items", 4, 0, 0, "26", "10/28/2026, 6:10 PM")]
for k, (job, table, runs, errors, empty, nrows, last) in enumerate(rows):
    y = 972 + k * 22
    t(38, y, job, 10); t(128, y, "c0ffee00-…-5e1d", 10, MUTED); t(300, y, table, 10)
    t(450, y, str(runs), 10, anchor="end")
    r(480, y - 12, 40, 16, fill="#107c10")
    t(500, y, str(errors), 10, "#ffffff", 600, "middle")
    t(575, y, str(empty), 10, anchor="end"); t(660, y, nrows, 10, anchor="end"); t(700, y, last, 10)
    ln(30, y + 8, 970, y + 8, GRID)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'font-family="{FONT}" role="img" aria-label="Mock-up of the cuscope Azure Workbook Overview tab: '
       f'summary tiles, smoothed utilisation chart, top contributors and harvester health. Illustrative data.">\n'
       + "\n".join(o) + "\n</svg>\n")
open(OUT, "w", encoding="utf-8").write(svg)
print(f"wrote {OUT} ({len(svg)} bytes)")
