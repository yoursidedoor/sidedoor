// Failures and logs > Run checks (E4.T4): [Copy report] copies the `sidedoor doctor` text.
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("doctor-copy");
  if (!button) return;
  button.addEventListener("click", function () {
    var report = document.getElementById(button.dataset.target);
    var said = document.getElementById("doctor-copied");
    var text = report ? report.textContent : "";
    function done(ok) { if (said) said.textContent = ok ? "Copied." : "Select the report and copy it."; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      var range = document.createRange();
      range.selectNodeContents(report);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      try { done(document.execCommand("copy")); } catch (error) { done(false); }
    }
  });
});
