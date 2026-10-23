// Report a problem (owner pick F3 a): build the privacy-safe summary on this computer, copy it, and
// open GitHub's bug-report form. Only the version and platform go in the address; the summary goes
// through the clipboard. Only ISSUE_FORM addresses are ever opened.
document.addEventListener("DOMContentLoaded", function () {
  var ISSUE_FORM = "https://github.com/yoursidedoor/sidedoor/issues/new?template=bug_report.yml&";
  var button = document.getElementById("report-problem-button");
  if (!button) return;
  var status = document.getElementById("report-problem-status");
  var fallback = document.getElementById("report-problem-fallback");
  var box = document.getElementById("report-problem-text");
  var link = document.getElementById("report-problem-link");
  function say(text) { if (status) status.textContent = text; }
  function copyByHand() {   // true only when the browser says the copy happened
    fallback.hidden = false;
    box.focus();
    box.select();
    try { return document.execCommand("copy"); } catch (error) { return false; }
  }
  button.addEventListener("click", function () {
    button.disabled = true;
    say("Building your summary...");
    fetch("/report-problem", { method: "POST", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) {
        return response.json().then(function (answer) { return { ok: response.ok, answer: answer }; },
                                     function () { return { ok: false, answer: {} }; });
      })
      .then(function (reply) {
        var answer = reply.answer || {};
        if (!reply.ok || typeof answer.summary !== "string") {
          // Refused (it couldn't be made safe) or failed: nothing is copied or opened.
          say(typeof answer.error === "string" ? answer.error : "Sidedoor couldn't build the summary. Try again in a moment.");
          return;
        }
        var text = answer.summary;
        var url = typeof answer.url === "string" && answer.url.indexOf(ISSUE_FORM) === 0 ? answer.url : null;
        box.value = text;
        if (url) link.href = url;
        // Both run after the fetch, so the browser may block either: each result is checked, and
        // "Copied" is said only when the copy really happened.
        function finish(copied) {
          var opened = null;
          if (url) {
            opened = window.open(url, "_blank");
            if (opened) { try { opened.opener = null; } catch (error) { /* already cut */ } }
          }
          if (copied && opened) {
            fallback.hidden = true;
            say("Copied. Paste it into the issue.");
            return;
          }
          fallback.hidden = false;
          link.hidden = !url;
          box.focus();
          box.select();
          if (copied) say("Copied. Click Open the GitHub issue form, then paste it into the issue.");
          else if (opened) say("Couldn't copy it. Copy the selected summary below and paste it into the issue.");
          else say("Click Open the GitHub issue form, then copy the selected summary below and paste it there.");
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { finish(true); }, function () { finish(copyByHand()); });
        } else {
          finish(copyByHand());
        }
      })
      .catch(function () { say("Sidedoor couldn't build the summary. Try again in a moment."); })
      .then(function () { button.disabled = false; });
  });
});
