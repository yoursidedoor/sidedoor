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
