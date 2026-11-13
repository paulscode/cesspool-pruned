// Cesspool Pruned: the Lightning pages. Loaded after site.js on installs that read a Lightning node
// (cesspool/lightning.py writes the data under /d/ln/, cesspool/pages_ln.py the pages).
//
// Everything on a page is built with createElement and textContent. A node's alias and colour are
// text its operator chose and anyone can announce anything, so neither is ever parsed as HTML: an
// alias is set as text, and a colour is used only when it is six hexadecimal digits.
(function () {
  "use strict";

  var GRADES = { pristine: "Pristine", tainted: "Tainted", foul: "Foul", raw: "Raw sewage" };
  var CLOSED = { mutual: "Cooperative close", force: "Forced close", penalty: "Forced close, with penalty", unknown: "Closed" };
  var CLOSED_NOTE = {
    mutual: "Both sides signed the transaction that spent the funding output: a cooperative close, or a splice into a new funding output.",
    force: "One side broadcast its commitment transaction.",
    penalty: "One side broadcast a revoked commitment transaction, and the other took the funds through the revocation key.",
    unknown: "The closing transaction is no longer stored here, so the kind of close is not known."
  };

  function el(tag, attrs) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "cls") e.className = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null || c === false) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }
  function num(x) { return Number(x).toLocaleString("en-US"); }
  function plural(k, word) { return num(k) + " " + word + (k === 1 ? "" : "s"); }
  function getJSON(url) {
    return fetch(url).then(function (r) { if (r.status === 404) return null; if (!r.ok) throw new Error(r.status); return r.json(); });
  }
  function when(t) { return new Date(t * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC"; }
  function sats(v) { return v >= 1e6 ? (v / 1e8).toFixed(v < 1e10 ? 4 : 2) + " BTC" : num(v) + " sat"; }
  // The same function as found.shard in Python: the low byte of the 32-bit FNV-1a hash, as two hex characters.
  function shard(s) {
    var h = 0x811c9dc5;
    for (var i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193);
    return ((h & 255) + 256).toString(16).slice(1);
  }
  function isPub(s) { return /^0[23][0-9a-f]{64}$/.test(s); }
  // 'HxTxO' from that form, or from the number LND shows (block height, transaction index and output index packed into 64 bits).
  function scid(s) {
    if (/^\d{1,8}x\d{1,8}x\d{1,5}$/.test(s)) return s.split("x").map(function (x) { return String(parseInt(x, 10)); }).join("x");
    if (/^\d{9,20}$/.test(s) && typeof BigInt === "function") {
      var n = BigInt(s);
      if (n >= (BigInt(1) << BigInt(64))) return null;
      return String(n >> BigInt(40)) + "x" + String((n >> BigInt(16)) & BigInt(0xffffff)) + "x" + String(n & BigInt(0xffff));
    }
    return null;
  }
  function lndId(id) {
    if (typeof BigInt !== "function") return "";
    var p = id.split("x");
    return String((BigInt(p[0]) << BigInt(40)) | (BigInt(p[1]) << BigInt(16)) | BigInt(p[2]));
  }
  function swatch(color) {
    var i = el("i", { cls: "lnsw" });
    if (/^#[0-9a-f]{6}$/.test(color || "")) i.style.background = color; else i.classList.add("none");
    return i;
  }
  function nodeLink(pub, alias, color) {
    var f = document.createDocumentFragment();
    f.appendChild(swatch(color));
    f.appendChild(el("a", { href: "/lightning/node/" + pub, text: alias || pub.slice(0, 16) + "…" }));
    return f;
  }
  function chanLink(id) { return el("a", { cls: "mono", href: "/lightning/channel/" + id, text: id }); }
  function say(out, text) { out.textContent = ""; out.appendChild(el("p", { cls: "muted", text: text })); }
  function statusWord(s, kind) { return s === "open" ? "Open" : s === "closed" ? (CLOSED[kind] || "Closed") : "No longer announced"; }

  // Whether this install has Lightning pages at all. A block page is drawn once and kept, so it cannot
  // know: Lightning may have been set up, or taken away, since. /d/features.json is written by every build.
  var onLightningPage = /^\/lightning\//.test(location.pathname);
  var features = onLightningPage ? Promise.resolve({ lightning: true })
    : getJSON("/d/features.json").then(function (f) { return f || {}; }).catch(function () { return {}; });
  features.then(function (f) {
    var nav = document.querySelector("nav.main"), link = nav && nav.querySelector('a[href="/lightning/"]');
    if (f.lightning && nav && !link) {
      var about = nav.querySelector('a[href="/about/"]');
      nav.insertBefore(el("a", { href: "/lightning/", text: "Lightning" }), about);
    } else if (!f.lightning && link) link.remove();
    if (f.lightning) start();
  });

  function start() {
  // The header box: a node's public key or a channel id opens its page. site.js handles everything
  // else, and would send these two to Your Blocks as names.
  var jump = document.querySelector(".jump");
  if (jump) jump.addEventListener("submit", function (e) {
    var v = jump.querySelector("input").value.trim(), low = v.toLowerCase();
    var to = isPub(low) ? "/lightning/node/" + low : (/x/.test(v) && scid(v)) ? "/lightning/channel/" + scid(v) : null;
    if (!to) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    location.href = to;
  }, true);

  // ---------------------------------------------------------------- /lightning/: find a node or a channel
  var search = document.querySelector("form.lnsearch");
  if (search) {
    var found = document.querySelector(".lnfound"), box = search.querySelector("input"), aliases = null;
    search.addEventListener("submit", function (e) {
      e.preventDefault();
      var v = box.value.trim(), low = v.toLowerCase();
      if (!v) return;
      if (isPub(low)) { location.href = "/lightning/node/" + low; return; }
      if (scid(v)) { location.href = "/lightning/channel/" + scid(v); return; }
      found.textContent = "Looking…";
      (aliases || (aliases = getJSON("/d/ln/alias.json"))).then(function (a) {
        found.textContent = "";
        if (!a) { say(found, "The list of names is not published yet. Try again in a minute."); return; }
        var exact = a[low] || [], part = [];
        if (!exact.length) Object.keys(a).forEach(function (name) { if (name.indexOf(low) >= 0) a[name].forEach(function (pub) { part.push([name, pub]); }); });
        if (exact.length === 1) { location.href = "/lightning/node/" + exact[0]; return; }
        var hits = exact.length ? exact.map(function (pub) { return [low, pub]; }) : part;
        if (!hits.length) { say(found, "No node with an open channel is named " + v + ". A public key is 66 hexadecimal characters; a channel id looks like 800000x1234x0."); return; }
        found.appendChild(el("p", { text: plural(hits.length, "node") + (exact.length ? " announce the name " : " have names containing ") + v + ". Anyone can announce any name." }));
        var ul = el("ul", { cls: "choices" });
        hits.slice(0, 60).forEach(function (h) {
          ul.appendChild(el("li", {}, el("a", { href: "/lightning/node/" + h[1], text: h[0] }), el("span", { cls: "muted mono small", text: " " + h[1].slice(0, 20) + "…" })));
        });
        found.appendChild(ul);
        if (hits.length > 60) found.appendChild(el("p", { cls: "muted small", text: "The first 60 of " + num(hits.length) + ". Type more of the name." }));
      }).catch(function () { aliases = null; say(found, "Could not load the list of names. Try again in a minute."); });
    });
  }

  // ---------------------------------------------------------------- /lightning/node/<key>
  var nodeApp = document.getElementById("lnnode");
  if (nodeApp) (function () {
    var out = nodeApp.querySelector(".lnout"), m = /^\/lightning\/node\/([0-9a-fA-F]{66})\/?$/.exec(location.pathname);
    if (!m) { say(out, "A node's page is at /lightning/node/ followed by its 66-character public key."); return; }
    var pub = m[1].toLowerCase();
    getJSON("/d/ln/n/" + pub.slice(2, 4) + ".json").then(function (file) {
      var node = file && file[pub];
      out.textContent = "";
      if (!node) {
        nodeApp.querySelector("h1").textContent = "Not in the graph";
        out.appendChild(el("p", {}, "No node with the key ", el("code", { cls: "ad", text: pub }), " is in the public graph this install has read."));
        return;
      }
      var name = node.al || pub.slice(0, 16) + "…";
      document.title = name + " · Lightning node · " + (document.body.getAttribute("data-brand") || "cesspool");
      var h1 = nodeApp.querySelector("h1");
      h1.textContent = "";
      h1.appendChild(swatch(node.co));
      h1.appendChild(document.createTextNode(name));
      out.appendChild(el("p", { cls: "mono small ad muted", text: pub }));
      if (node.al) out.appendChild(el("p", { cls: "small muted", text: "The name is whatever the node announces. Anyone can announce any name; the key is the identity." }));
      var open = node.ch.filter(function (c) { return c[4] === "open"; }), shut = node.ch.filter(function (c) { return c[4] === "closed"; });
      var gone = node.ch.filter(function (c) { return c[4] === "gone"; });
      var tiles = el("div", { cls: "tiles" });
      tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: sats(node.cap) }), el("div", { cls: "l", text: "capacity of its open channels" })));
      tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: num(open.length) }), el("div", { cls: "l", text: "open public channels" }),
        el("div", { cls: "s", text: num(gone.length) + " more no longer announced" })));
      tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: num(shut.length) }), el("div", { cls: "l", text: "closed in blocks graded here" })));
      if (node.f) tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: when(node.f).slice(0, 10) }), el("div", { cls: "l", text: "first seen by this install" })));
      out.appendChild(tiles);
      if (node.ad && node.ad.length) {
        var ul = el("ul", { cls: "choices" });
        node.ad.forEach(function (a) { ul.appendChild(el("li", { cls: "mono small ad", text: a })); });
        out.appendChild(el("h3", { text: "Announced addresses" }));
        out.appendChild(ul);
      }
      var chart = capacityChart(node.ch);
      if (chart) { out.appendChild(el("h3", { text: "Capacity by block height" })); out.appendChild(chart); }
      out.appendChild(el("h3", { text: "Channels" }));
      out.appendChild(channelTable(node.ch));
    }).catch(function () { say(out, "Could not load the node. Try again in a minute."); });
  })();

  // The capacity of a node's channels as blocks went by: up at each funding block, down at each close
  // seen here. Drawn from the node's own channel list, so it covers the channels this install knows.
  function capacityChart(chs) {
    var events = [];
    chs.forEach(function (c) {
      var h = parseInt(c[0].split("x")[0], 10);
      events.push([h, c[3]]);
      if (c[4] === "closed" && c[6]) events.push([c[6], -c[3]]);
    });
    if (events.length < 2) return null;
    events.sort(function (a, b) { return a[0] - b[0]; });
    var lo = events[0][0], hi = Math.max(events[events.length - 1][0], lo + 1), total = 0, top = 0, pts = [];
    events.forEach(function (e) { pts.push([e[0], total]); total += e[1]; pts.push([e[0], total]); if (total > top) top = total; });
    pts.push([hi, total]);
    if (!top) return null;
    var W = 1000, H = 160, ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("class", "lnchart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Capacity of this node's channels from block " + lo + " to block " + hi);
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + ((p[0] - lo) / (hi - lo) * W).toFixed(1) + " " + (H - 4 - p[1] / top * (H - 12)).toFixed(1); }).join(" ");
    var area = document.createElementNS(ns, "path");
    area.setAttribute("d", d + " L" + W + " " + (H - 4) + " L0 " + (H - 4) + " Z");
    area.setAttribute("class", "a");
    var line = document.createElementNS(ns, "path");
    line.setAttribute("d", d);
    line.setAttribute("class", "l");
    svg.appendChild(area);
    svg.appendChild(line);
    return el("div", {}, el("div", { cls: "tw" }, svg),
      el("div", { cls: "small faint", text: "Block " + num(lo) + " to block " + num(hi) + ", up to " + sats(top) + ". Closes count only where this install graded the block." }));
  }

  var PAGE = 100;
  function channelTable(chs) {
    var t = el("table"), shown = 0, more = el("button", { cls: "btn ghost", type: "button" });
    t.appendChild(el("tr", {}, el("th", { text: "Channel" }), el("th", { text: "With" }), el("th", { cls: "r", text: "Capacity" }), el("th", { text: "Status" }), el("th", { text: "Funded in block" })));
    function page() {
      chs.slice(shown, shown + PAGE).forEach(function (c) {
        var h = c[0].split("x")[0];
        t.appendChild(el("tr", {}, el("td", {}, chanLink(c[0])), el("td", {}, nodeLink(c[1], c[2])), el("td", { cls: "r num", text: sats(c[3]) }),
          el("td", { text: statusWord(c[4], c[5]) }), el("td", {}, el("a", { href: "/block/" + h + "/", text: h }))));
      });
      shown = Math.min(chs.length, shown + PAGE);
      more.textContent = "Show " + num(Math.min(PAGE, chs.length - shown)) + " more";
      more.hidden = shown >= chs.length;
    }
    more.addEventListener("click", page);
    page();
    return el("div", {}, el("div", { cls: "tw" }, t), el("p", { style: "margin-top:12px" }, more));
  }

  // ---------------------------------------------------------------- /lightning/channel/<id>
  var chanApp = document.getElementById("lnchan");
  if (chanApp) (function () {
    var out = chanApp.querySelector(".lnout"), m = /^\/lightning\/channel\/([0-9x]+)\/?$/.exec(location.pathname), id = m && scid(m[1]);
    if (!id) { say(out, "A channel's page is at /lightning/channel/ followed by its id, such as 800000x1234x0."); return; }
    var parts = id.split("x"), height = parseInt(parts[0], 10), k = Math.floor(height / 1000);
    Promise.all([getJSON("/d/ln/c/" + k + ".json"), getJSON("/d/s/" + k + ".json")]).then(function (r) {
      var c = r[0] && r[0][id], blocks = r[1] || {};
      out.textContent = "";
      chanApp.querySelector("h1").textContent = "Channel " + id;
      if (!c) {
        out.appendChild(el("p", {}, "No channel ", el("code", { text: id }), " is in the public graph this install has read. A channel that closed before this install first read the graph was never seen here."));
        return;
      }
      document.title = "Channel " + id + " · " + (document.body.getAttribute("data-brand") || "cesspool");
      var lnd = lndId(id);
      out.appendChild(el("p", { cls: "small muted" }, "Block " + num(height) + ", transaction " + num(parts[1]) + ", output " + parts[2] + (lnd ? ". LND writes the same id as " + lnd + "." : ".")));
      var tiles = el("div", { cls: "tiles" });
      tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: sats(c.c) }), el("div", { cls: "l", text: "capacity" })));
      var x = c.x;
      tiles.appendChild(el("div", { cls: "tile" }, el("div", { cls: "v", text: c.s === "open" ? "Open" : c.s === "closed" ? "Closed" : "Not announced" }),
        el("div", { cls: "l", text: c.s === "open" ? "in the graph at the last read" : c.s === "closed" ? (CLOSED[x && x.k] || "Closed").toLowerCase() : "left the graph, no close seen here" })));
      out.appendChild(tiles);
      var pair = el("div", { cls: "grid2" });
      [["a", "aa", "pa"], ["b", "ba", "pb"]].forEach(function (side) {
        var p = c[side[2]], panel = el("div", { cls: "panel" }, el("div", { cls: "kicker", text: "One end" }), el("h3", {}, nodeLink(c[side[0]], c[side[1]])),
          el("p", { cls: "mono small ad muted", text: c[side[0]] }));
        if (p) {
          panel.appendChild(el("p", { cls: "small" }, "To forward a payment out through this channel it charges " + num(p[0] / 1000) + " sat plus " + num(p[1]) + " parts in a million, asks for " + num(p[2]) + " blocks of time lock, and takes payments from " +
            num(Math.floor(p[3] / 1000)) + " to " + num(Math.floor(p[4] / 1000)) + " sat." + (p[5] ? " It has the channel switched off for now." : "")));
        } else panel.appendChild(el("p", { cls: "small muted", text: "It has announced no fees for this channel." }));
        pair.appendChild(panel);
      });
      out.appendChild(pair);
      out.appendChild(el("h3", { text: "Funding" }));
      var fund = el("p", {});
      if (c.p) {
        var txid = c.p.split(":")[0];
        fund.appendChild(document.createTextNode("Output " + c.p.split(":")[1] + " of "));
        fund.appendChild(el("a", { cls: "mono small", href: "/tx/" + txid, text: txid.slice(0, 16) + "…" }));
        fund.appendChild(document.createTextNode(", in block "));
      } else fund.appendChild(document.createTextNode("Transaction " + num(parts[1]) + " of block "));
      fund.appendChild(blockRef(height, blocks[height]));
      out.appendChild(fund);
      if (!blocks[height]) out.appendChild(el("p", { cls: "small muted", text: "This install has not graded block " + height + ", so the funding transaction has no page here." }));
      if (x) {
        out.appendChild(el("h3", { text: CLOSED[x.k] || "Closed" }));
        var cl = el("p", {}, CLOSED_NOTE[x.k] + " The closing transaction is ", el("a", { cls: "mono small", href: "/tx/" + x.t, text: x.t.slice(0, 16) + "…" }), ", in block ");
        out.appendChild(cl);
        getJSON("/d/s/" + Math.floor(x.h / 1000) + ".json").then(function (b) { cl.appendChild(blockRef(x.h, b && b[x.h])); cl.appendChild(document.createTextNode(".")); })
          .catch(function () { cl.appendChild(el("a", { href: "/block/" + x.h + "/", text: String(x.h) })); });
        if (x.pen) out.appendChild(el("p", {}, "The penalty transaction is ", el("a", { cls: "mono small", href: "/tx/" + x.pen, text: x.pen.slice(0, 16) + "…" }), "."));
      } else if (c.s === "gone") {
        out.appendChild(el("p", { cls: "small muted", text: "It was last in the graph on " + when(c.l) + ". If it closed, it closed in a block this install has not graded." }));
      }
    }).catch(function () { say(out, "Could not load the channel. Try again in a minute."); });
  })();

  // A block by height, with its grade when this install has graded it. s is its row of /d/s/<k>.json.
  function blockRef(h, s) {
    var f = document.createDocumentFragment();
    f.appendChild(el("a", { href: "/block/" + h + "/", text: String(h) }));
    if (s) {
      f.appendChild(document.createTextNode(" "));
      f.appendChild(el("span", { cls: "stamp sm g-" + s[1], text: GRADES[s[1]] || s[1] }));
      f.appendChild(el("span", { cls: "muted small", text: " " + when(s[0]) }));
    }
    return f;
  }

  // ---------------------------------------------------------------- a transaction page: does it open or close a channel?
  var txout = document.querySelector("#txapp .txout");
  if (txout) {
    var noted = "";
    var annotate = function () {
      var idEl = txout.querySelector(".dis .txid"), id = idEl && idEl.textContent.trim();
      if (!id || id === noted || !/^[0-9a-f]{64}$/.test(id)) return;
      noted = id;
      getJSON("/d/ln/tx/" + shard(id) + ".json").then(function (file) {
        var rows = file && file[id], sec = txout.querySelector(".dis");
        if (!rows || !sec || noted !== id) return;
        var box = el("div", { cls: "note ln" });
        rows.forEach(function (r) {
          var line = el("p", { cls: "small" });
          var what = r[1] === "open" ? "Opens Lightning channel " : r[1] === "penalty" ? "Takes the funds of Lightning channel " : "Closes Lightning channel ";
          line.appendChild(el("b", { text: what }));
          line.appendChild(chanLink(r[0]));
          line.appendChild(document.createTextNode(" between " + r[2][0] + " and " + r[2][1] +
            (r[1] === "close" ? " (" + (CLOSED[r[3]] || "closed").toLowerCase() + ")" : r[1] === "penalty" ? " through the revocation key, after a revoked commitment was broadcast" : "") + "."));
          box.appendChild(line);
        });
        var meta = sec.querySelector(".meta");
        if (meta && meta.nextSibling) sec.insertBefore(box, meta.nextSibling); else sec.appendChild(box);
      }).catch(function () {});
    };
    new MutationObserver(annotate).observe(txout, { childList: true, subtree: true });
    annotate();
  }

  // ---------------------------------------------------------------- a block page: channels opened and closed in it
  var bm = /^\/block\/(\d+)\/?$/.exec(location.pathname), bhead = document.querySelector(".bhead .t");
  if (bm && bhead) {
    var bh = parseInt(bm[1], 10), bk = Math.floor(bh / 1000);
    Promise.all([getJSON("/d/ln/c/" + bk + ".json"), getJSON("/d/ln/x/" + bk + ".json")]).then(function (r) {
      var opened = Object.keys(r[0] || {}).filter(function (id) { return parseInt(id.split("x")[0], 10) === bh; });
      var closed = (r[1] && r[1][bh]) || [];
      if (!opened.length && !closed.length) return;
      var p = el("p", { cls: "small lnblock" }, el("b", { text: "Lightning. " }));
      var list = function (ids, label) {
        p.appendChild(document.createTextNode(label));
        ids.slice(0, 12).forEach(function (id, i) { if (i) p.appendChild(document.createTextNode(", ")); p.appendChild(chanLink(id)); });
        if (ids.length > 12) p.appendChild(document.createTextNode(" and " + num(ids.length - 12) + " more"));
        p.appendChild(document.createTextNode(". "));
      };
      if (opened.length) list(opened, plural(opened.length, "public channel") + " funded in this block: ");
      if (closed.length) list(closed.map(function (c) { return c[0]; }), plural(closed.length, "channel") + " closed in it: ");
      bhead.appendChild(p);
    }).catch(function () {});
  }
  }
})();
