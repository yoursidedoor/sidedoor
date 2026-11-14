// The live view (E15.T5): the page in Sidedoor's browser, shown on a canvas; the person's mouse, wheel, keys
// and paste go back over the dashboard's own socket. Loaded by the page that embeds the fragment
// (templates/live_view_fragment.html); fragments swapped in later are picked up on htmx:load.
// Keys are sent as the key and its PHYSICAL code (event.code); the server works out the key code from that.
// Nothing typed here is kept, logged or shown anywhere on this page.
(function () {
  "use strict";

  function setUp(root) {
    if (root.dataset.liveReady) return;
    root.dataset.liveReady = "1";
    var canvas = root.querySelector("[data-live-canvas]"), status = root.querySelector("[data-live-status]");
    // Inside a card the view has no start button of its own (the card's button is the one control for the
    // step): a stand-in keeps the lines below the same, and the card's button is held while the view is
    // opening or open, so the step cannot be pressed a second time.
    var startButton = root.querySelector("[data-live-start]") || {hidden: true, disabled: false, addEventListener: function () {}};
    var doneButton = root.querySelector("[data-live-done]");
    var card = root.closest ? root.closest(".access-card") : null;
    var cardStep = card ? card.querySelector('form[action^="/access/step"] button') : null;
    function hold(on) { if (cardStep) cardStep.disabled = on; }
    var cancelButton = root.querySelector("[data-live-cancel]"), context = canvas.getContext("2d");
    var site = root.querySelector("[data-live-site]");
    var target = root.dataset.target, socket = null, session = null, over = false, starting = false, misses = 0;
    var size = {width: 1280, height: 900}, lastButton = "none", clicks = 0, lastDown = 0, pendingMove = null;

    var noticeUntil = 0;
    function say(words) { if (Date.now() >= noticeUntil) status.textContent = words; }
    // A sentence that must stay readable for a few seconds, while the status line keeps arriving every second.
    function notice(words) { noticeUntil = 0; say(words); noticeUntil = Date.now() + 8000; }
    // The site being shown, above the picture at all times. Always as text, never as markup.
    function saySite(words) { site.textContent = words || ""; }
    function live(on) {
      canvas.hidden = !on; doneButton.hidden = !on; cancelButton.hidden = !on; startButton.hidden = on;
      if (on) canvas.focus();
    }
    // E16.T2: a press on the site's Submit was not passed on: the card's red line shows it for a moment.
    function flashWarning() {
      var card = root.closest ? root.closest(".access-card") : null;
      var line = card ? card.querySelector(".access-warning") : null;
      if (!line) return;
      line.classList.add("flash");
      setTimeout(function () { line.classList.remove("flash"); }, 1200);
    }

    function send(message) { if (socket && socket.readyState === 1) socket.send(JSON.stringify(message)); }

    function connect() {
      if (over || !session) return;
      var scheme = location.protocol === "https:" ? "wss://" : "ws://";
      socket = new WebSocket(scheme + location.host + "/live-view/ws?s=" + encodeURIComponent(session));
      socket.binaryType = "blob";
      socket.onopen = function () { misses = 0; };
      socket.onmessage = function (event) {
        if (typeof event.data !== "string") { draw(event.data); return; }
        var message = JSON.parse(event.data);
        if (message.t === "meta") { size = {width: message.width || size.width, height: message.height || size.height}; }
        else if (message.t === "status") { say(message.words); saySite(message.site); }
        else if (message.t === "site") { saySite(message.words); }
        else if (message.t === "refused") { flashWarning(); }
        else if (message.t === "ended") { finish(message.words); if (!asked) closed(message.reason); }
        // An attached view whose session is over (E16.T2): the card's section is drawn again (it listens).
        if (message.t === "ended" && message.reason === "closed") root.dispatchEvent(new CustomEvent("live-view-over", {bubbles: true}));
      };
      socket.onclose = function () {
        if (over) return;
        misses += 1;
        // A dropped socket comes back by itself, to the same session only: nothing here ever starts a new
        // one (that takes the person's click). After a few tries the session is over.
        if (misses > 6) { finish(root.dataset.over); return; }
        say(root.dataset.reconnecting);
        setTimeout(connect, 1500);
      };
    }
    function draw(blob) {
      createImageBitmap(blob).then(function (picture) {
        if (canvas.width !== picture.width || canvas.height !== picture.height) {
          canvas.width = picture.width; canvas.height = picture.height;
        }
        canvas.style.aspectRatio = size.width + " / " + size.height;
        context.drawImage(picture, 0, 0);
        picture.close();
      }, function () {});
    }
    function post(path) {
      return fetch(path, {method: "POST", credentials: "same-origin"}).then(function (response) {
        return response.json().then(function (body) { return {ok: response.ok, body: body}; });
      });
    }
    function finish(words) {
      over = true; starting = false; live(false); hold(false); noticeUntil = 0; say(words); saySite("");
      startButton.hidden = false; startButton.disabled = false; cancelButton.disabled = false;
      if (socket) socket.close();
      root.dispatchEvent(new CustomEvent("live-view-ended", {bubbles: true}));
    }
    // The person finished the step (Done) and the server has finished with it: the list the card is in is
    // drawn again (the card listens for this), since the application no longer waits. Never for any other
    // ending or a start that failed: the card stays as it is, with this view's sentence to be read.
    var asked = false;
    function closed(reason) {
      if (reason === "done" || reason === "finished") root.dispatchEvent(new CustomEvent("live-view-done", {bubbles: true}));
    }
    function start() {
      if (starting) return;
      starting = true; over = false; misses = 0; startButton.disabled = true; hold(true); noticeUntil = 0; say(root.dataset.opening);
      cancelButton.hidden = false;   // a page that is slow to open can be cancelled
      post("/live-view/" + encodeURIComponent(target) + "/start").then(function (answer) {
        if (!starting) return;       // cancelled meanwhile
        starting = false;
        if (answer.body.state !== "live") {
          finish(answer.body.words || root.dataset.notShown);
          if (answer.body.again === false) startButton.hidden = true;   // nothing to try again on this computer
          return;
        }
        session = answer.body.session; live(true); say(answer.body.words); connect();
      }, function () { if (starting) finish(root.dataset.notShown); });
    }
    function end(reason) {
      doneButton.disabled = true; cancelButton.disabled = true; asked = true;
      // Owner, 2026-11-13: Done closes the view on the press itself. The picture and its buttons go now and the
      // done line shows; the server finishes behind it exactly as before (it keeps the site's session from the
      // live browser first, then closes it), and the card is drawn again when that answer comes.
      var closedEarly = reason === "done" && root.dataset.done;
      if (closedEarly) { live(false); startButton.hidden = true; notice(root.dataset.done); }
      // The answer comes once the server has finished with the step (saved, released): only then the card redraws.
      post("/live-view/end?reason=" + reason).then(function (answer) {
        doneButton.disabled = false; cancelButton.disabled = false; asked = false;
        if (!over) finish(answer.body.words || root.dataset.over);
        closed(answer.body.reason);
      }, function () {
        doneButton.disabled = false; cancelButton.disabled = false; asked = false;
        if (closedEarly && !over) { noticeUntil = 0; live(true); }   // the press did not reach Sidedoor: the view is back
      });
    }
    startButton.addEventListener("click", start);
    doneButton.addEventListener("click", function () { end("done"); });
    cancelButton.addEventListener("click", function () { end("cancel"); });

    // --- the person's input ---
    function at(event) {
      var box = canvas.getBoundingClientRect();
      return {x: (event.clientX - box.left) * size.width / box.width, y: (event.clientY - box.top) * size.height / box.height};
    }
    function mods(event) {
      return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
    }
    function button(number) { return ["left", "middle", "right"][number] || "none"; }
    canvas.addEventListener("mousemove", function (event) {
      var point = at(event), first = pendingMove === null;
      pendingMove = {t: "mouse", type: "mouseMoved", x: point.x, y: point.y, mod: mods(event), button: lastButton, buttons: event.buttons};
      if (first) requestAnimationFrame(function () { var move = pendingMove; pendingMove = null; if (move) send(move); });
    });
    canvas.addEventListener("mousedown", function (event) {
      canvas.focus();
      var point = at(event), now = Date.now();
      lastButton = button(event.button);
      clicks = now - lastDown < 400 ? Math.min(clicks + 1, 3) : 1; lastDown = now;
      send({t: "mouse", type: "mousePressed", x: point.x, y: point.y, mod: mods(event), button: lastButton, buttons: event.buttons, clicks: clicks});
      event.preventDefault();
    });
    canvas.addEventListener("mouseup", function (event) {
      var point = at(event);
      send({t: "mouse", type: "mouseReleased", x: point.x, y: point.y, mod: mods(event), button: button(event.button), buttons: event.buttons, clicks: clicks});
      lastButton = "none";
    });
    canvas.addEventListener("contextmenu", function (event) { event.preventDefault(); });
    canvas.addEventListener("wheel", function (event) {
      var point = at(event);
      send({t: "wheel", x: point.x, y: point.y, dx: event.deltaX, dy: event.deltaY, mod: mods(event)});
      event.preventDefault();
    }, {passive: false});
    function pasteKey(event) { return (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "v"; }
    canvas.addEventListener("keydown", function (event) {
      if (pasteKey(event)) return;   // the paste event carries the text
      event.preventDefault();
      send({t: "key", type: "down", key: event.key, code: event.code, mod: mods(event)});
    });
    canvas.addEventListener("keyup", function (event) {
      if (pasteKey(event)) return;
      event.preventDefault();
      send({t: "key", type: "up", key: event.key, code: event.code, mod: mods(event)});
    });
    root.ownerDocument.addEventListener("paste", function (event) {
      if (root.ownerDocument.activeElement !== canvas) return;
      var text = event.clipboardData.getData("text");
      event.preventDefault();
      // Too long for the view: said in words and never sent (the server would drop it without a word).
      if (text.length > Number(root.dataset.pasteMax)) { notice(root.dataset.pasteLong); return; }
      send({t: "paste", text: text});
    });

    if (root.hasAttribute("data-autostart")) start();
  }

  function scan(scope) {
    var found = (scope || document).querySelectorAll("[data-live-view]");
    for (var index = 0; index < found.length; index++) setUp(found[index]);
    if (scope && scope.matches && scope.matches("[data-live-view]")) setUp(scope);
  }
  document.addEventListener("DOMContentLoaded", function () { scan(document); });
  document.addEventListener("htmx:load", function (event) { scan(event.target); });
})();
