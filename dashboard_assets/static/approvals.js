// Approvals (E6.T4): the live character count of a message box, and its one button's label. A
// connection note over LinkedIn's 300 characters turns the button off until it fits; the text stays
// as typed. Once the box differs from the message as written, the button says "Approve my edit":
// whatever the box says is what is approved. Newlines count as one character, as the server counts
// them (it turns CRLF into LF before counting and storing).
document.addEventListener("input", function (event) {
  var box = event.target;
  if (!box.matches || !box.matches("textarea[data-count]")) return;
  var count = document.getElementById(box.getAttribute("data-count"));
  var button = document.getElementById(box.getAttribute("data-approve"));
  var limit = parseInt(box.getAttribute("data-limit"), 10);
  var value = box.value.replace(/\r\n?/g, "\n");
  var length = Array.from(value).length;
  var over = !isNaN(limit) && length > limit;
  if (count) {
    count.textContent = length + (isNaN(limit) ? "" : " / " + limit) + " characters" +
      (over ? ". LinkedIn allows " + limit + " in a connection note: shorten it by " + (length - limit) + "." : "");
    count.classList.toggle("field-error", over);
  }
  if (button) {
    button.disabled = over;
    var original = (box.getAttribute("data-original") || "").replace(/\r\n?/g, "\n");
    button.textContent = value === original ? "Approve and send" : "Approve my edit";
  }
});

// Applications waiting (owner, 2026-10-24): "Cancel" puts the shown answers back (a form reset) and
// leaves edit mode (the CSS toggle is a checkbox on the card). The approve button says "Approve my
// edits and ..." once anything on the card differs from what was shown, else "Approve and ...".
document.addEventListener("click", function (event) {
  var cancel = event.target.closest && event.target.closest(".approval-a-cancel");
  if (!cancel) return;
  var card = cancel.closest("article");
  var toggle = card.querySelector(".approval-a-editing");
  if (toggle) setTimeout(function () { toggle.checked = false; card.classList.remove("approval-a-changed"); }, 0);
});

function applicationChanged(card) {
  var controls = card.querySelectorAll(".approval-a-control, .approval-a-choices input");
  for (var i = 0; i < controls.length; i++) {
    var c = controls[i];
    if (c.type === "checkbox" || c.type === "radio") { if (c.checked !== c.defaultChecked) return true; }
    else if (c.tagName === "SELECT") {
      for (var j = 0; j < c.options.length; j++) if (c.options[j].selected !== c.options[j].defaultSelected) return true;
    } else if (c.value !== c.defaultValue) return true;
  }
  return false;
}

["input", "change"].forEach(function (kind) {
  document.addEventListener(kind, function (event) {
    var card = event.target.closest && event.target.closest(".approval-app");
    if (!card || !event.target.matches(".approval-a-control, .approval-a-choices input")) return;
    card.classList.toggle("approval-a-changed", applicationChanged(card));
  });
});
