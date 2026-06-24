(function () {
  const POLL_MS = 5000;
  let timer = null;

  function refreshPollFragments() {
    if (typeof htmx === "undefined" || document.hidden) {
      return;
    }
    document.querySelectorAll("[data-poll-fragment]").forEach(function (node) {
      const url = node.getAttribute("hx-get");
      if (!url) {
        return;
      }
      htmx.ajax("GET", url, { target: node, swap: "innerHTML" });
    });
  }

  function syncPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (!document.hidden) {
      refreshPollFragments();
      timer = setInterval(refreshPollFragments, POLL_MS);
    }
  }

  document.addEventListener("visibilitychange", syncPolling);
  document.addEventListener("DOMContentLoaded", syncPolling);
})();
