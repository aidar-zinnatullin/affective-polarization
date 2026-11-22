"""Build self-contained charts from the CSV snapshot, including the first paint."""
import csv
from collections import Counter, defaultdict
from html import escape
from pathlib import Path

# Figures 3–5 use the same five driver colors for bars, lines, and points.
DRIVER_COLORS = {
    "Psychological factors": "#0000FF",
    "Media and information environment": "#FFA500",
    "Identity and group processes": "#00FF00",
    "Institutional factors": "#FF0000",
    "Socioeconomic factors": "#FFFF00",
}
QUARTILE_COLORS = {"Q1": "#FFA500", "Q2": "#FFFF00"}
NEUTRAL = "#707070"
FORMS = ["All forms", "Horizontal", "Vertical", "Both", "Unspecified"]


def read(path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def chooser(key, labels, panels, legend):
    items = []
    css = []
    output = []
    for i, (label, panel) in enumerate(zip(labels, panels)):
        rid = f"{key}-{i}"
        items.append(f'<input type="radio" name="{key}" id="{rid}" {"checked" if i == 0 else ""}>'
                     f'<label for="{rid}">{escape(label)}</label>')
        css.append(f'#{rid}:checked ~ .panels > .{rid} {{display:block}}')
        output.append(f'<div class="{rid}">{panel}</div>')
    return ('<fieldset class="ap-switch"><legend>' + legend + '</legend><style>' + ''.join(css)
            + '</style>' + ''.join(items) + '<div class="panels">' + ''.join(output) + '</div></fieldset>')


def trend(cells, families, form):
    totals = Counter()
    for (yr, family, outcome), n in cells.items():
        if form == "All forms" or form == outcome:
            totals[yr, family] += n
    if not sum(totals.values()):
        return '<p>No articles in this selection yet.</p>'
    years = list(range(min(y for y,_ in totals), max(y for y,_ in totals) + 1))
    maximum = max(totals.values())
    step = max(1, (maximum + 4)//5)
    high = ((maximum + step - 1)//step)*step
    def x(i): return 76 + 686*i/(len(years)-1) if len(years)>1 else 405
    def y(n): return 222 - 192*n/high
    title = f'{form} · {sum(totals.values())} articles'
    svg = [f'<svg viewBox="0 0 810 265" role="img" aria-label="{title}, articles by year and driver">'
           f'<title>{title}</title><text x="18" y="16">Articles</text>']
    for n in range(0, high+1, step):
        svg.append(f'<path d="M64 {y(n)} H775" stroke="#dee5e0"/><text x="53" y="{y(n)+4}" text-anchor="end">{n}</text>')
    for i,yr in enumerate(years):
        if i % max(1,(len(years)+11)//12)==0 or i==len(years)-1:
            svg.append(f'<text x="{x(i):.1f}" y="247" text-anchor="middle">{yr}</text>')
    for family in families:
        color = DRIVER_COLORS[family]
        pts = [(x(i), y(totals[yr,family])) for i,yr in enumerate(years)]
        points = ' '.join(f'{a:.1f},{b:.1f}' for a,b in pts)
        svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.8"/>')
        for yr,(a,b) in zip(years,pts):
            svg.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.2" fill="{color}"><title>{yr} · {family}: {totals[yr,family]}</title></circle>')
    legend = ''.join(f'<span><i style="background:{DRIVER_COLORS[family]}"></i>{family}</span>' for family in families)
    header = ''.join(f'<th scope="col">{f}</th>' for f in families)
    body = ''.join('<tr>'+f'<th scope="row">{yr}</th>'+''.join(f'<td>{totals[yr,f]}</td>' for f in families)+'</tr>' for yr in years)
    return (f'<p class="ap-total">{title}</p>'+''.join(svg)+'</svg><div class="legend">'+legend+
            '</div><details><summary>Annual counts</summary><div class="ap-scroll"><table><thead><tr><th>Year</th>'+
            header+'</tr></thead><tbody>'+body+'</tbody></table></div></details>')


def bar_color(measure, category):
    if measure == "Driver family":
        return DRIVER_COLORS[category]
    if measure == "Journal quartile":
        return QUARTILE_COLORS.get(category, NEUTRAL)
    if measure in ("Outcome", "Design"):
        return "#0000FF"  # Figure 2a–b
    return NEUTRAL  # Table 1 gives geographic counts without a color key.


def bars(values, measure):
    if not sum(values.values()):
        return '<p>No articles in this selection yet.</p>'
    peak = max(values.values())
    return '<div class="bars">'+''.join(
        f'<div class="barrow"><span>{escape(k)}</span><div class="track"><div class="bar" style="width:{100*v/peak:.2f}%;background:{bar_color(measure, k)}"></div></div><strong>{v}</strong></div>'
        for k,v in values.items())+'</div>'


def build(app):
    baseline = read(app/'baseline.csv')
    families = [r['category'] for r in baseline if r['measure']=='Driver family']
    annual = read(app/'annual.csv')
    studies = read(app/'studies.csv')
    included = [r for r in studies if r['inclusion_basis']]
    verified = [r for r in included if r['inclusion_basis'] == 'Reviewer']
    ai = [r for r in included if r['inclusion_basis'] == 'AI']
    pending = [r for r in studies if r['status'] == 'AI proposal: check outlet'
               and 'Primary' in r['queries'].split(' | ')]
    corpora = [
        ("Published + AI-reviewed", True, included),
        ("Published baseline", True, []),
        ("AI-reviewed additions", False, ai),
        ("Published + verified", True, verified),
        ("Verified additions", False, verified),
        ("Pending outlet checks", False, pending),
    ]
    results = []
    for i, (label, with_baseline, extra) in enumerate(corpora):
        counts = defaultdict(Counter)
        cells = Counter()
        for r in baseline:
            counts[r['measure']][r['category']] = int(r['n']) if with_baseline else 0
        if with_baseline:
            for r in annual:
                cells[int(r['year']),r['driver'],r['outcome']] += int(r['n'])
        for r in extra:
            cells[int(r['date'][:4]), r['driver'], r['outcome']] += 1
            for measure, field in [('Driver family','driver'),('Design','design'),('Outcome','outcome'),('Journal quartile','quartile')]:
                counts[measure][r[field] or 'Pending journal check'] += 1
            areas = list(dict.fromkeys(a.strip() for a in r['countries'].split('|') if a.strip()))
            counts['Geography'].update(['Cross-national (>2)'] if len(areas)>2 else areas or ['None'])
        forms = chooser(f'ap-form{i}', FORMS, [trend(cells,families,f) for f in FORMS], 'Outcome form')
        measures = chooser(f'ap-measure{i}',list(counts),[bars(values, measure) for measure, values in counts.items()], 'Breakdown')
        caption = ''
        if i == 0:
            baseline_n = sum(int(r['n']) for r in annual)
            caption = (f'<p class="foot">{baseline_n} published articles · {len(ai)} AI-reviewed additions'
                       f' · {len(verified)} reviewer-confirmed additions</p>')
        elif label == "Pending outlet checks":
            caption = '<p class="foot">These candidates await confirmation that their journal is indexed in SCImago.</p>'
        results.append('<section class="section"><h2>Research over time</h2>'+caption+forms+
                       '</section><section class="section"><h2>Article counts</h2>'+measures+'</section>')
    return ('<div class="ap-evidence">'+chooser('ap-corpus', [c[0] for c in corpora], results, 'Corpus')+
            '<p class="foot">Published search through 22 August 2025. The 2025 baseline covers part of the year.</p></div>')
