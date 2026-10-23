// Settings (E6.T8): the eye button beside a key box shows or hides the key (owner, 2026-10-22:
// "keys should be shown but hidden"). Hidden by default. Delegated, so boxes htmx swaps in work too.
document.addEventListener("click", function (event) {
  var button = event.target.closest("button[data-reveal]");
  if (!button) return;
  var input = document.getElementById(button.getAttribute("data-reveal"));
  if (!input) return;
  var show = input.type === "password";
  input.type = show ? "text" : "password";
  button.setAttribute("aria-pressed", show ? "true" : "false");
  button.setAttribute("aria-label", show ? "Hide key" : "Show key");
});
