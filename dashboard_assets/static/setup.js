// Setup (round 3, owner 2026-11-08): a step done by an action collapses after a few seconds
// (setup_step.html's .step-autocollapse), but never once the user has typed, pressed a key or
// clicked inside that step since it was swapped in. Focus alone does not count: htmx may put focus
// back after an Enter-submit, which is not the user doing anything (round 4).
["input", "keydown", "pointerdown"].forEach(function (kind) {
  document.addEventListener(kind, function (event) {
    if (!event.isTrusted) return;
    var step = event.target.closest && event.target.closest("li.step");
    if (step) step.setAttribute("data-dirty", "1");
  }, true);
});
document.addEventListener("htmx:confirm", function (event) {
  var element = event.detail && event.detail.elt;
  if (!element || !element.classList || !element.classList.contains("step-autocollapse")) return;
  var step = element.closest("li.step");
  if (step && step.getAttribute("data-dirty")) event.preventDefault();
});
