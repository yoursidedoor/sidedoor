// Email support (owner, 2026-10-24): ask Sidedoor to start an email to support in the user's webmail
// (POST /report-problem/email), copy the same summary the GitHub button copies, and show the draft's
// status (GET /report-problem/email) until the session has ended: it could not be started, or its draft
// window was closed (idle with `last`, which a poll that never saw "open" still reads). While the user
// signs in to their webmail ("signin") the page keeps polling. The button is usable again once the
// draft is open. Nothing is sent: the user presses Send in their own browser.
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("support-email-button");
  if (!button) return;
  var line = document.getElementById("support-email-status");
  var fallback = document.getElementById("report-problem-fallback");
  var box = document.getElementById("report-problem-text");
  var link = document.getElementById("report-problem-link");
  var copied = false;
  var timer = null;
  function show(text, extra) {
    line.hidden = false;
    line.textContent = text;
    if (extra) line.appendChild(extra);
  }
  function settingsLink(href) {
    var span = document.createElement("span");
    span.appendChild(document.createTextNode(" "));
    var anchor = document.createElement("a");
    anchor.href = href.indexOf("/settings") === 0 ? href : "/settings";
    anchor.textContent = "Open Settings";
    span.appendChild(anchor);
    return span;
  }
  function copyByHand() {
    fallback.hidden = false;
    link.hidden = true;
    box.focus();
    box.select();
    try { return document.execCommand("copy"); } catch (error) { return false; }
  }
  function copy(text) {
    box.value = text;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return copyByHand(); });
    }
    return Promise.resolve(copyByHand());
  }
  function render(answer) {
    var state = answer.state;
    var message = typeof answer.message === "string" ? answer.message : "";
    if (state === "no_webmail") {
      show(message, settingsLink(answer.settings || "/settings"));
    } else if (state === "failed" || state === "busy") {
      var address = typeof answer.address === "string" ? answer.address : "support@yoursidedoor.com";
      var after = copied ? " Your summary is copied. You can paste it into an email to " + address + " yourself."
                         : " Copy the summary below and paste it into an email to " + address + " yourself.";
      if (!copied) { fallback.hidden = false; link.hidden = true; }
      show(message + after, typeof answer.settings === "string" ? settingsLink(answer.settings) : null);
    } else if (message) {
      show(message);
    }
    return state === "starting" || state === "signin" || state === "open";
  }
  function settle(answer) {
    var more = render(answer);
    button.disabled = answer.state === "starting" || answer.state === "signin";
    if (more) poll();
  }
  function poll() {
    timer = window.setTimeout(function () {
      fetch("/report-problem/email", { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (response) { return response.json(); })
        .then(function (answer) {
          settle(answer || {});
        })
        .catch(function () { poll(); });
    }, 2000);
  }
  button.addEventListener("click", function () {
    button.disabled = true;
    if (timer) window.clearTimeout(timer);
    copied = false;
    show("Opening your email...");
    fetch("/report-problem/email", { method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        return response.json().then(function (answer) { return answer || {}; }, function () { return {}; });
      })
      .then(function (answer) {
        if (typeof answer.summary !== "string" && !answer.state) {
          show(typeof answer.error === "string" ? answer.error : "Sidedoor couldn't build the summary. Try again in a moment.");
          button.disabled = false;
          return;
        }
        var copying = typeof answer.summary === "string" ? copy(answer.summary) : Promise.resolve(false);
        return copying.then(function (done) {
          copied = !!done;
          settle(answer);
        });
      })
      .catch(function () {
        show("Sidedoor couldn't start the email. Try again in a moment.");
        button.disabled = false;
      });
  });
});
