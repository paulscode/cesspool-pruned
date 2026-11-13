"""The Lightning pages' HTML. lightning.py works out what is on them; this draws it, with the pieces
site.py already has: the page frame, tiles, tables, the column chart's styles.

Aliases and colours are other people's text. An alias goes through esc() wherever it is written, and a
colour is used only after lightning.py has checked that it is six hexadecimal digits.
"""
import datetime, os

from . import lightning, local
from .types import KNOWN

FULL = 500  # rows on a ranking's own page
CLOSE_WORDS = {"mutual": "Cooperative", "force": "Forced", "penalty": "Forced, with penalty", "unknown": "Unknown"}
CLOSE_NOTE = {"mutual": "both sides signed: a cooperative close, or a splice into a new funding output",
              "force": "one side broadcast its commitment transaction",
              "penalty": "one side broadcast a revoked commitment and the other took the funds",
              "unknown": "the closing transaction is no longer stored here"}


def swatch(color):
    return f'<i class="lnsw" style="background:{color}"></i>' if color else '<i class="lnsw none"></i>'


def node_link(site, pub, alias, color=""):
    return f'{swatch(color)}<a href="/lightning/node/{pub}">{site.esc(alias)}</a>'


def chan_link(scid):
    return f'<a class="mono" href="/lightning/channel/{scid}">{scid}</a>'


def sats(site, v):
    """A capacity: BTC to four places from a hundredth of a coin up, sats below that."""
    return f"{site.btc(v, 4 if v < 10**10 else 2)} BTC" if v >= 10**6 else f"{site.n(v)} sat"


def series_chart(site, stats, pick, label, fmt):
    """One column a day for the days on file, thinned to at most 30, in the styles of the pool chart."""
    if len(stats) < 2:
        return '<p class="small muted">A chart appears here once there are two days to compare. One reading is kept a day, from the day Lightning was set up.</p>'
    rows = stats if len(stats) <= 30 else [stats[round(i * (len(stats) - 1) / 29)] for i in range(30)]
    top = max(r[pick] for r in rows) or 1
    cols = []
    for r in rows:
        day = datetime.date.fromisoformat(r[0])
        cols.append(f'<div class="wk" title="{r[0]}: {fmt(r[pick])}"><div class="wk-v">{fmt(r[pick])}</div>'
                    f'<div class="wk-plot"><div class="wk-bar ln" style="height:{max(1.5, 100 * r[pick] / top):.1f}%"></div></div>'
                    f'<div class="wk-d">{day:%b} {day.day}</div></div>')
    return f'<div class="tw"><div class="wk-chart" role="img" aria-label="{site.esc(label)}, one column a day, oldest first">{"".join(cols)}</div></div>'


def rank_table(site, rows, limit):
    body = "".join(f'<tr><td class="rank">{i + 1}</td><td>{node_link(site, pub, alias, color)}</td><td class="r num">{sats(site, cap)}</td>'
                   f'<td class="r num">{site.n(count)}</td><td class="r num">{site.n(old) if old else ""}</td></tr>'
                   for i, (pub, alias, color, cap, count, old) in enumerate(rows[:limit]))
    return (f'<div class="tw"><table><tr><th>#</th><th>Node</th><th class="r">Capacity</th><th class="r">Channels</th>'
            f'<th class="r">Oldest channel, block</th></tr>{body or "<tr><td colspan=5 class=muted>None.</td></tr>"}</table></div>')


def close_rows(site, nodes, closes, limit):
    rows = []
    for scid, a, b, cap, x in closes[:limit]:
        when = site.tm(x["at"], False) if x.get("at") else ""
        rows.append(f'<tr><td>{chan_link(scid)}</td><td class="wrapc">{node_link(site, a, lightning.alias_of(nodes, a))} and {node_link(site, b, lightning.alias_of(nodes, b))}</td>'
                    f'<td class="r num">{sats(site, cap)}</td><td>{CLOSE_WORDS[x["k"]]}</td>'
                    f'<td><a href="/block/{x["h"]}/">{x["h"]}</a> <span class="faint">{when}</span></td>'
                    f'<td class="mono small"><a href="/tx/{x["t"]}">{x["t"][:16]}…</a></td></tr>')
    return "".join(rows)


CLOSE_HEAD = '<tr><th>Channel</th><th>Between</th><th class="r">Capacity</th><th>Closed</th><th>In block</th><th>Closing transaction</th></tr>'


def your_node(site, i):
    me = i.get("node") or {}
    if not me.get("pub"):
        return ""
    name = site.esc(i.get("name") or "Lightning node")
    ok = lambda v: '<span class="t-down">yes</span>' if v else '<span class="t-up">not yet</span>'
    return f'''<div class="panel"><div class="kicker">Your node · {name}</div>
<h3 style="font-size:1.3rem">{node_link(site, me["pub"], me.get("alias") or me["pub"][:16], me.get("color", ""))}</h3>
<p class="mono small ad muted">{me["pub"]}</p>
<p class="small" style="margin:0">{site.esc(me.get("version", ""))}{" · " if me.get("version") else ""}block {site.n(me.get("height", 0))} · in step with the chain: {ok(me.get("chain_synced"))} · with the graph: {ok(me.get("graph_synced"))}</p>
<p class="small muted" style="margin:8px 0 0">Only what the node announces to everyone is read: the public graph, and this identity. No balances, payments or private channels.</p></div>'''


def filters_section(site, total, flagged, blocks_read, idx):
    """What each node policy does with the Lightning transactions in the blocks graded here."""
    names = site.VERDICT_NAMES
    cols = {"core": 1, "knots": 2, "plumb": 3}
    head = "".join(f'<th class="r">{site.esc(name)} refuses</th>' for _, name in names)
    rows = []
    for kind in lightning.KINDS:
        e = total.get(kind)
        if not e:
            continue
        cells = "".join(f'<td class="r num">{site.n(e[cols[k]])}</td>' for k, _ in names)
        rows.append(f'<tr><td class="wrapc">{site.esc(KNOWN[kind][0])}</td><td class="r num">{site.n(e[0])}</td>{cells}'
                    f'<td class="r num">{site.n(e[4])}</td><td class="r num">{site.n(e[5])}</td></tr>')
    table = (f'<div class="tw"><table><tr><th>Lightning transaction</th><th class="r">Seen</th>{head}<th class="r">Gray</th><th class="r">Sewage</th></tr>'
             f'{"".join(rows) or "<tr><td colspan=9 class=muted>None in the blocks read so far.</td></tr>"}</table></div>')
    frows = []
    for h, txid, kind, tier, reasons in flagged[:60]:
        why = "; ".join(f'{site.esc(dict(names).get(k, k))}: {", ".join(f"<code>{site.esc(r)}</code>" for r in rs)}' for k, rs in reasons.items() if k in dict(names))
        frows.append(f'<tr><td><a href="/block/{h}/">{h}</a></td><td class="mono small"><a href="/tx/{txid}">{txid[:16]}…</a></td>'
                     f'<td>{site.esc(KNOWN[kind][0]) if kind in KNOWN else site.esc(kind)}</td><td>{site.esc(tier)}</td><td class="wrapc small">{why}</td></tr>')
    listed = (f'<h3 style="margin-top:18px">Lightning transactions a default policy refuses, or that grade gray or sewage</h3>'
              f'<div class="tw"><table><tr><th>Block</th><th>Transaction</th><th>Kind</th><th>Graded</th><th>Refused by</th></tr>{"".join(frows)}</table></div>'
              + (f'<p class="small muted">The newest 60 of {site.n(len(flagged))}.</p>' if len(flagged) > 60 else "")) if frows else ""
    behind = len(idx) - blocks_read
    lag = f" {site.n(behind)} older blocks are still to be read for this table." if behind > 0 else ""
    return f'''<h2 id="filters">Lightning and the filters</h2>
<p style="max-width:75ch">A filter that refuses spam must not refuse Lightning. These are the Lightning transactions in the {site.n(blocks_read)} blocks read here so far, found by their scripts, with how many of them each policy refuses at its defaults. A refusal here is one to look at: a node that refuses a force close cannot relay it.{lag} <a href="/about/#known">How protocols are recognized</a>.</p>
{table}{listed}'''


def index_page(site, page, tip, summary, closes, nodes, per, filt, i, idx):
    t = summary["totals"]
    stats = summary["stats"]
    chain = local.chain_name()
    name = site.esc(i.get("name") or "your Lightning node")
    read = site.tm(i["read"]) if i.get("read") else "never"
    live = [p for p, e in per.items() if e["n"]]
    has_addr = [p for p in live if (nodes.get(p) or {}).get("ad")]
    tor = sum(1 for p in has_addr if all(".onion" in a for a in nodes[p]["ad"]))
    stale = "" if i.get("ok") else (f'<p class="note small">The last read of the graph failed: {site.esc(i.get("error") or "unknown reason")}. '
                                    f'What is below is from {read}.</p>')
    tiles = f'''<div class="tiles"><div class="tile"><div class="v">{site.n(t["nodes"])}</div><div class="l">nodes with an open channel</div>
<div class="s">{site.pct(tor / len(has_addr)) if has_addr else "0%"} of those with an address are reachable over Tor only</div></div>
<div class="tile"><div class="v">{site.n(t["channels"])}</div><div class="l">public channels open</div><div class="s">{site.n(t["gone"])} more no longer announced</div></div>
<div class="tile"><div class="v">{site.btc(t["capacity"], 2)}</div><div class="l">BTC of capacity</div></div>
<div class="tile"><div class="v">{site.n(t["avg"])}</div><div class="l">sat in the average channel</div><div class="s">{site.n(t["median"])} in the median one</div></div></div>'''
    ck = summary.get("checked") or {"ok": 0, "bad": 0}
    checked = ""
    if ck["bad"]:
        checked = (f'<p class="note small">{site.n(ck["bad"])} of the {site.n(ck["ok"] + ck["bad"])} channels funded in blocks graded here do not match those blocks: '
                   f'the transaction at the place the channel&#39;s id names is not its funding transaction, or does not hold its capacity. '
                   f'That means {name} and this install&#39;s Bitcoin node disagree about a block.</p>')
    elif ck["ok"]:
        checked = (f'<p class="small muted">{site.n(ck["ok"])} channel{"s" if ck["ok"] != 1 else ""} funded in blocks graded here {"were" if ck["ok"] != 1 else "was"} checked against '
                   f'{"those blocks" if ck["ok"] != 1 else "its block"}: each id names its funding transaction, and the output holds the capacity announced.</p>')
    kinds = summary["closes"]
    seen = sum(kinds.values())
    closed = f'''<h2 id="closed">Channels closed in graded blocks</h2>
<p style="max-width:75ch">A channel closes when its funding output is spent. Every input of every block graded here is on file, so a close shows up the moment its block is graded, with the kind of close read from the transaction itself. Closes in blocks this install has not graded are not seen: such a channel is counted above as no longer announced.</p>
<div class="tiles"><div class="tile"><div class="v">{site.n(kinds["mutual"])}</div><div class="l">cooperative</div><div class="s">{CLOSE_NOTE["mutual"]}</div></div>
<div class="tile"><div class="v">{site.n(kinds["force"])}</div><div class="l">forced</div><div class="s">{CLOSE_NOTE["force"]}</div></div>
<div class="tile"><div class="v{" g-raw" if kinds["penalty"] else ""}">{site.n(kinds["penalty"])}</div><div class="l">forced, with a penalty</div><div class="s"><a href="/lightning/penalties/">the list</a></div></div></div>
{f'<div class="tw"><table>{CLOSE_HEAD}{close_rows(site, nodes, closes, 15)}</table></div>' if seen else '<p class="muted">None seen yet.</p>'}
{f'<p class="small"><a href="/lightning/closed/">All {site.n(seen)} &rarr;</a></p>' if seen > 15 else ""}'''
    tops = summary["tops"]
    body = f'''<div class="hero"><div class="kicker">Lightning · {chain} chain</div><h1>The pipes</h1>
<p class="lede">The public Lightning network as {name} on this server sees it, joined to the blocks graded here: which block funded each channel, how each one closed, and what the spam filters do with Lightning's own transactions.</p>
<p class="small muted" style="margin-top:8px">Graph read {read}; it is read again every {max(1, lightning.REFRESH // 60)} minutes.</p></div>
{stale}
<form class="txform lnsearch" role="search"><input spellcheck="false" autocomplete="off" placeholder="Node name, public key, or channel id" aria-label="Find a Lightning node by name or public key, or a channel by its id"><button class="btn">Find</button></form>
<div class="lnfound" aria-live="polite"></div>
{tiles}
{checked}
<div class="grid2" style="margin-top:14px"><div><h3>Capacity, BTC</h3>{series_chart(site, stats, 3, "Total capacity of open public channels", lambda v: f"{v / 1e8:,.0f}" if v >= 10**10 else f"{v / 1e8:,.2f}")}</div>
<div><h3>Channels</h3>{series_chart(site, stats, 2, "Open public channels", site.n)}</div></div>
<div style="margin-top:18px">{your_node(site, i)}</div>
<h2 id="nodes">Largest nodes</h2>
<div class="tabs" data-group="lnrank"><button data-pane="ln-capacity" class=on>By capacity</button><button data-pane="ln-channels">By channels</button><button data-pane="ln-oldest">Oldest channel</button></div>
<div class="tabpane on" id="ln-capacity" data-group="lnrank">{rank_table(site, tops["capacity"], 10)}<p class="small"><a href="/lightning/nodes/capacity/">The first {FULL} by capacity &rarr;</a></p></div>
<div class="tabpane" id="ln-channels" data-group="lnrank">{rank_table(site, tops["channels"], 10)}<p class="small"><a href="/lightning/nodes/channels/">The first {FULL} by channels &rarr;</a></p></div>
<div class="tabpane" id="ln-oldest" data-group="lnrank">{rank_table(site, tops["oldest"], 10)}<p class="small"><a href="/lightning/nodes/oldest/">The first {FULL} by oldest channel &rarr;</a></p></div>
{closed}
{filters_section(site, *filt, idx)}
<p class="small muted" style="margin-top:26px">A node is not graded here, and neither is a channel: there is no measure for that. Maps and lists by country or network provider are left out, because they need a location database this install does not carry.</p>'''
    return page("/lightning/", "Lightning", body, nav="lightning",
                desc=f"The public Lightning network on the {chain} chain as this server's node sees it: nodes, channels, closes and what the spam filters do with them.", tip=tip)


RANKS = {"capacity": ("by capacity", lambda e: -e["cap"]), "channels": ("by number of channels", lambda e: -e["n"]),
         "oldest": ("by oldest open channel", lambda e: e["old"])}


def rank_page(site, page, tip, key, nodes, per):
    words, order = RANKS[key]
    live = sorted((p for p, e in per.items() if e["n"]), key=lambda p: (order(per[p]), p))
    rows = [[p, lightning.alias_of(nodes, p), (nodes.get(p) or {}).get("co", ""), per[p]["cap"], per[p]["n"], per[p]["old"]] for p in live[:FULL]]
    others = " · ".join(f'<a href="/lightning/nodes/{k}/">{w}</a>' for k, (w, _) in RANKS.items() if k != key)
    body = f'''<div class="hero"><div class="kicker">Lightning</div><h1>Nodes, {words}</h1>
<p class="lede">The first {site.n(len(rows))} of {site.n(len(live))} nodes with an open public channel. Capacity is the sum of a node's open channels, each counted whole on both of its ends.</p>
<p class="small"><a href="/lightning/">&larr; Lightning</a> · Also {others}</p></div>
{rank_table(site, rows, FULL)}'''
    return page(f"/lightning/nodes/{key}/", f"Lightning nodes {words}", body, nav="lightning", desc=f"Lightning nodes ranked {words}.", tip=tip)


def closes_page(site, page, tip, nodes, closes, only=None):
    rows = [c for c in closes if c[4]["k"] == only] if only else closes
    if only == "penalty":
        path, title, lede = "/lightning/penalties/", "Penalties", (
            "A channel's two sides each hold a commitment transaction, and replace it every time the balance moves. Broadcasting an old one is an attempt to "
            "take back what was already paid away, and the other side may then take everything through the revocation key. These are the channels where that "
            "happened in a block graded here: a forced close, then a spend of its output down the revocation path.")
        extra = "".join(
            f'<tr><td>{chan_link(scid)}</td><td class="mono small"><a href="/tx/{x["pen"]}">{x["pen"][:16]}…</a></td><td><a href="/block/{x.get("penh", x["h"])}/">{x.get("penh", x["h"])}</a></td></tr>'
            for scid, a, b, cap, x in rows[:FULL] if x.get("pen"))
        tail = (f'<h3 style="margin-top:22px">The penalty transactions</h3><div class="tw"><table><tr><th>Channel</th><th>Penalty transaction</th><th>In block</th></tr>{extra}</table></div>'
                '<p class="small muted" style="margin-top:8px">A penalty is recognized by the BOLT 3 to_local script spent through its revocation branch. '
                'A revoked HTLC output taken the same way, and the penalty spend of a taproot channel, are not read as penalties here.</p>') if extra else ""
    else:
        path, title, lede, tail = "/lightning/closed/", "Closed channels", (
            "Every channel whose funding output was spent in a block graded here, newest first."), ""
    table = f'<div class="tw"><table>{CLOSE_HEAD}{close_rows(site, nodes, rows, FULL)}</table></div>' if rows else '<p class="muted">None seen in the blocks graded here.</p>'
    more = f'<p class="small muted">The newest {FULL} of {site.n(len(rows))}.</p>' if len(rows) > FULL else ""
    body = f'''<div class="hero"><div class="kicker">Lightning</div><h1>{title}</h1><p class="lede">{lede}</p>
<p class="small"><a href="/lightning/">&larr; Lightning</a></p></div>{table}{more}{tail}'''
    return page(path, f"Lightning: {title.lower()}", body, nav="lightning", desc=lede[:150], tip=tip)


def shell(site, page, tip, what):
    """The node page or the channel page: one file each, filled in by static/lightning.js from /d/ln/."""
    if what == "node":
        body = '''<div id="lnnode"><div class="hero"><div class="kicker">Lightning node</div><h1>Node</h1></div>
<div class="lnout" aria-live="polite">Loading…</div>
<noscript><p class="muted">This page needs JavaScript to load the node.</p></noscript></div>'''
        return page("/lightning/node/", "Lightning node", body, nav="lightning", desc="A Lightning node: its channels, capacity and the blocks that funded and closed them.", tip=tip)
    body = '''<div id="lnchan"><div class="hero"><div class="kicker">Lightning channel</div><h1>Channel</h1></div>
<div class="lnout" aria-live="polite">Loading…</div>
<noscript><p class="muted">This page needs JavaScript to load the channel.</p></noscript></div>'''
    return page("/lightning/channel/", "Lightning channel", body, nav="lightning", desc="A Lightning channel: its two nodes, its funding transaction and how it closed.", tip=tip)


def write_all(out, idx, tip, page, summary, closes, nodes, chans, per, filt, i):
    from . import site
    site.write(out, "lightning/index.html", index_page(site, page, tip, summary, closes, nodes, per, filt, i, idx))
    for key in RANKS:
        site.write(out, f"lightning/nodes/{key}/index.html", rank_page(site, page, tip, key, nodes, per))
    site.write(out, "lightning/closed/index.html", closes_page(site, page, tip, nodes, closes))
    site.write(out, "lightning/penalties/index.html", closes_page(site, page, tip, nodes, closes, "penalty"))
    site.write(out, "lightning/node/index.html", shell(site, page, tip, "node"))
    site.write(out, "lightning/channel/index.html", shell(site, page, tip, "channel"))
