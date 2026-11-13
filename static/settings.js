// Cesspool Pruned: the settings form on /about/#settings (cesspool/local.py settings_html), on platforms
// with no settings form of their own. It shows the settings in force now, from /status.json, and sends
// a change to /settings (tools/settings_server.py). The updater picks it up within a minute.
(function () {
  "use strict";
  var form = document.querySelector("form.settingsform");
  if (!form) return;
  var out = form.querySelector(".settingsout"), select = form.querySelector("select[name=max_gb]");
  var used = +form.getAttribute("data-bytes") || 0;
  function say(text, bad) { out.textContent = text; out.classList.toggle("bad", !!bad); }
  function show(s) {
    if (!s) return;
    form.querySelectorAll("input[name=backfill]").forEach(function (r) { r.checked = (r.value === "1") === !!s.backfill; });
    if (select.querySelector('option[value="' + s.max_gb + '"]')) select.value = String(s.max_gb);
  }
  function gb(v) { return v ? v + " GB" : "no limit"; }

  fetch("/status.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (st) {
    if (!st) return;
    if (st.bytes) used = st.bytes;
    show(st.settings);
  }).catch(function () {});

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var on = form.querySelector("input[name=backfill]:checked");
    var want = { backfill: !!on && on.value === "1", max_gb: parseInt(select.value, 10) };
    if (want.max_gb && used > want.max_gb * 1073741824) {
      var over = ((used - want.max_gb * 1073741824) / 1073741824).toFixed(1);
      if (!window.confirm("This chain's blocks take " + (used / 1073741824).toFixed(1) + " GB now. Lowering the limit to " + gb(want.max_gb) +
        " deletes about " + over + " GB of the oldest graded blocks, and on a pruned node they cannot be graded again unless they are rebuilt. Go ahead?")) return;
    }
    say("Saving…");
    fetch("/settings", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Cesspool-Settings": "1" },
      body: JSON.stringify(want)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j, status: r.status }; });
    }).then(function (res) {
      if (!res.ok) { say("Not saved: " + (res.j.error || "the server answered " + res.status) + ".", true); return; }
      show(res.j.settings);
      say("Saved. It takes effect within a minute; this page shows what changed after the next update.");
    }).catch(function () { say("Not saved: the server could not be reached. Try again in a minute.", true); });
  });
})();
