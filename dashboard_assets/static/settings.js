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

// M4 (2026-11-12): Enter on a tick box marked data-enter-inert does nothing (Space toggles it). The box
// saves on change in a small form of its own, and Enter there sent that form unchanged and reloaded
// the page. Without JavaScript the form's Save button still sends it.
document.addEventListener("keydown", function (event) {
  if (event.key !== "Enter" || !event.target.matches) return;
  if (event.target.matches("input[type=checkbox][data-enter-inert]")) event.preventDefault();
});
