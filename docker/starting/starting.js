// The page shown before the first build: say what the updater is doing, and open the site when it is there.
(function () {
  "use strict";
  var message = document.getElementById("message"), detail = document.getElementById("detail");
  function check() {
    fetch("/tip.json?m=" + Date.now(), { cache: "no-store" }).then(function (r) {
      // The starting page itself answers every path until the site exists, so look at what came back.
      return r.ok ? r.text() : "";
    }).then(function (text) {
      if (text.charAt(0) === "{") { location.replace("/"); return; }
      return fetch("/status.json?m=" + Date.now(), { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (s) {
        if (!s) { message.textContent = "Starting"; detail.textContent = ""; return; }
        message.textContent = s.message || "Starting";
        detail.textContent = s.state === "error" ? "It keeps trying by itself." : "";
      });
    }).catch(function () {});
  }
  check();
  setInterval(check, 4000);
})();
