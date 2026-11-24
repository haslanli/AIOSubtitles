// AIOSubtitles' pages: the few moments that move. Every page works without this script: the sign-up then opens by its
// link and shows its questions one under another. With it, the sign-up is a layer that slides in, one question per
// screen, Back (its arrow, or the phone's) or Escape reversing it; the language slots show one place and
// "Add language" up to the instance's limit; passwords can be shown; the personal link can be copied; a setup page
// whose work runs shows its progress without loading itself again. It loads nothing but that page's own address, sends
// nothing and stores nothing.
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function each(selector, fn, scope) {
    Array.prototype.forEach.call((scope || document).querySelectorAll(selector), fn);
  }
  function later(fn) { setTimeout(fn, still ? 0 : 60); }

  // ---------------------------------------------------------------------------------------------- the sign-up layer
  var body = document.body;
  var layer = document.getElementById("signup");
  var form = document.querySelector("form[data-signup]");
  if (layer && form) {
    var screens = Array.prototype.slice.call(form.querySelectorAll("[data-screen]"));
    var segments = Array.prototype.slice.call(form.querySelectorAll(".steps li"));
    var behind = [document.querySelector(".wrap"), document.querySelector(".dock")].filter(Boolean);
    var at = 0, opener = null;
    // Back goes where the arrow goes: each screen the layer moves to is an entry in the page's history (its depth: how
    // many the layer added), so the phone's Back button or gesture shows the screen before, and from the first one
    // closes the layer, the answers still in it. A page loaded again starts closed, whatever its entry held
    var depth = function () { return (history.state && history.state.depth) || 0; };
    var enter = function (to) {
      history.pushState({ screen: to, depth: depth() + 1 }, "");
      at = to;
    };
    var earlier = depth();
    if (earlier) history.replaceState(null, "");

    var paint = function (back, focusProblem, box) {
      screens.forEach(function (s, i) { s.hidden = i !== at; });
      var screen = screens[at], step = +screen.getAttribute("data-step"), fill = screen.getAttribute("data-fill");
      if (!still) {
        screen.classList.remove("in", "back");
        void screen.offsetWidth;
        screen.classList.add("in");
        if (back) screen.classList.add("back");
      }
      segments.forEach(function (li, i) {
        li.classList.toggle("done", i < step);
        li.classList.toggle("f100", i < step || (i === step && fill === "100"));
        li.classList.toggle("f50", i === step && fill === "50");
        if (i === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      });
      form.scrollTop = 0;
      var problem = document.getElementById("problem");
      // the box a problem's link names, scrolled into view as it takes focus; else the screen's field, or its heading
      // when it asks with choices (as the approved pieces do)
      var target = (focusProblem && problem) || box || screen.querySelector("input.input, h2.q");
      if (target && target.classList.contains("q")) target.tabIndex = -1;
      if (target) later(function () { target.focus({ preventScroll: target !== box }); });
    };
    var open = function (from, name, focusProblem) {
      opener = from || opener;
      var found = screens.map(function (s) { return s.getAttribute("data-screen"); }).indexOf(name);
      enter(found < 0 ? 0 : found);
      body.classList.add("open");
      layer.setAttribute("aria-hidden", "false");
      behind.forEach(function (el) { el.inert = true; });
      paint(false, focusProblem);
    };
    var close = function () {
      body.classList.remove("open");
      body.removeAttribute("data-open-at");
      layer.setAttribute("aria-hidden", "true");
      behind.forEach(function (el) { el.inert = false; });
      if (location.hash === "#signup") history.replaceState(null, "", location.pathname + location.search);
      var visible = function (el) { return el && el.offsetParent !== null; };
      var back = visible(opener) ? opener : Array.prototype.filter.call(document.querySelectorAll("[data-open]"), visible)[0];
      if (back) back.focus({ preventScroll: true });
    };

    document.addEventListener("click", function (e) {
      var t = e.target.closest("[data-open], [data-back], [data-next]");
      if (!t) return;
      if (t.hasAttribute("data-open")) { e.preventDefault(); open(t); }
      else if (t.hasAttribute("data-back") && layer.contains(t)) { e.preventDefault(); history.back(); }
      else if (t.hasAttribute("data-next")) { enter(Math.min(at + 1, screens.length - 1)); paint(false); }
    });
    window.addEventListener("popstate", function (e) {
      if (!body.classList.contains("open")) return;
      var to = e.state && e.state.screen;
      if (typeof to !== "number") { close(); return; }
      var back = to < at;
      at = to;
      paint(back);
    });
    // Enter (or a phone keyboard's Go) in any field submits the form: before the last screen that is Continue; on the
    // last it is Sign up, sent once, so a second press doesn't start a second sign-up (C2-0546)
    var sent = false;
    form.addEventListener("submit", function (e) {
      if (at === screens.length - 1) {
        if (sent) e.preventDefault();
        sent = true;
        return;
      }
      e.preventDefault();
      enter(at + 1);
      paint(false);
    });
    // a page the browser shows again from its history (Back after a sign-up that didn't come back) can be sent again
    window.addEventListener("pageshow", function () { sent = false; });
    // Escape closes the layer at once, then takes the page back past the entries it added
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !body.classList.contains("open")) return;
      var added = depth();
      close();
      if (added) history.go(-added);
    });
    // after a problem the page comes back with the layer open at the screen it names (the first one it emptied) and the
    // summary focused; a link to #signup opens it
    var openAt = body.getAttribute("data-open-at");
    if (openAt) open(null, openAt, true);
    else if (location.hash === "#signup") {
      history.replaceState(null, "", location.pathname + location.search);
      open(document.querySelector(".go [data-open]"));
    } else {
      layer.setAttribute("aria-hidden", "true");
      screens.forEach(function (s, i) { s.hidden = i !== 0; });
      if (earlier) history.go(-earlier);      // back past the entries an earlier sign-up on this page added
    }
    // a link in the problem summary goes to the screen with its field and focuses it: a box itself, a group of answers
    // at its picked one, else its first
    each("#problem a[href^='#']", function (a) {
      a.addEventListener("click", function (e) {
        var field = document.getElementById(a.getAttribute("href").slice(1));
        var screen = field && field.closest("[data-screen]");
        if (!screen) return;
        e.preventDefault();
        enter(screens.indexOf(screen));
        paint(false, false, field.matches("input, select") ? field
          : field.querySelector("input:checked") || field.querySelector("input"));
      });
    });
  }

  // ---------------------------------------------------------------------------------------------- a choice that needs another
  each("[data-needs]", function (row) {
    var needed = document.getElementById(row.getAttribute("data-needs"));
    var box = row.querySelector("input");
    if (!needed || !box) return;
    var sync = function () {
      row.classList.toggle("off", !needed.checked);
      box.disabled = !needed.checked;
      if (!needed.checked) box.checked = false;
    };
    needed.addEventListener("change", sync);
    sync();
  });

  // ---------------------------------------------------------------------------------------------- the language slots
  each(".slots[data-max-languages]", function (group) {
    var max = parseInt(group.getAttribute("data-max-languages"), 10);   // always written: the instance's limit
    var removeWords = group.getAttribute("data-remove-label");           // always written: a remove button's words
    var rows = Array.prototype.slice.call(group.querySelectorAll("[data-slot]"));
    var selects = rows.map(function (r) { return r.querySelector("select"); });
    var add = group.parentNode.querySelector("[data-add]");
    var shown = Math.max(1, selects.reduce(function (n, s, i) { return s.value ? i + 1 : n; }, 0));

    var nameOf = function (select) {
      var option = select.options[select.selectedIndex];
      return select.value && option ? option.text : rows[selects.indexOf(select)].querySelector("label").textContent;
    };
    var paint = function () {
      rows.forEach(function (row, i) { row.hidden = i >= shown; });
      selects.forEach(function (select, i) {
        select.classList.toggle("empty", !select.value);
        Array.prototype.forEach.call(select.options, function (option) {
          option.disabled = !!option.value && selects.some(function (other, j) {
            return j !== i && j < shown && other.value === option.value;
          });
        });
        var remove = rows[i].querySelector("[data-remove-slot]");
        if (remove) {
          remove.hidden = false;
          remove.setAttribute("aria-label", removeWords + nameOf(select));
        }
      });
      if (add) add.hidden = shown >= Math.min(max, rows.length);
    };
    group.addEventListener("change", function (e) {
      if (e.target.matches("select")) paint();
    });
    group.addEventListener("click", function (e) {
      var remove = e.target.closest("[data-remove-slot]");
      if (!remove) return;
      var i = rows.indexOf(remove.closest("[data-slot]"));
      for (var j = i; j < shown - 1; j += 1) selects[j].value = selects[j + 1].value;
      selects[shown - 1].value = "";
      shown -= 1;
      paint();
      (add && !add.hidden ? add : selects[Math.max(0, i - 1)]).focus();
    });
    if (add) {
      add.addEventListener("click", function () {
        if (shown >= Math.min(max, rows.length)) return;
        shown += 1;
        var row = rows[shown - 1];
        selects[shown - 1].value = "";
        paint();
        if (!still) {
          row.classList.remove("new");
          void row.offsetWidth;
          row.classList.add("new");
        }
        selects[shown - 1].focus();
      });
    }
    paint();
  });

  // ---------------------------------------------------------------------------------------------- show a password
  each("button[data-reveal]", function (button) {
    var box = document.getElementById(button.getAttribute("data-reveal"));
    if (!box) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      var showing = box.type === "text";
      box.type = showing ? "password" : "text";
      button.textContent = button.getAttribute(showing ? "data-show" : "data-hide");
      button.setAttribute("aria-label", button.getAttribute(showing ? "data-show-label" : "data-hide-label"));
    });
  });

  // ---------------------------------------------------------------------------------------------- copy the personal link
  each("button[data-copy]", function (button) {
    var box = document.getElementById(button.getAttribute("data-copy"));
    var status = document.getElementById(button.getAttribute("data-copy") + "-status");
    var label = button.querySelector("span");
    if (!box || !label || !navigator.clipboard || !window.isSecureContext) return;
    var words = label.textContent, timer = 0;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(box.value || box.textContent).then(function () {
        label.textContent = button.getAttribute("data-done");
        if (status) status.textContent = button.getAttribute("data-done");
        clearTimeout(timer);
        timer = setTimeout(function () {
          label.textContent = words;
          if (status) status.textContent = "";
        }, 1800);
      }, function () {
        box.focus();
        box.select();
      });
    });
  });

  // ---------------------------------------------------------------------------------------------- a problem after a form
  var problem = document.getElementById("problem");
  if (problem && !(layer && layer.contains(problem))) problem.focus();
  // without one, the note a save came back with: focused too, so a screen reader reads it first
  var saved = !problem && document.getElementById("saved");
  if (saved) saved.focus();

  // ---------------------------------------------------------------------------------------------- work as it goes
  // A setup page whose work runs (data-refresh) loads itself again every few seconds without the script. With it, the
  // page fetches itself and puts in only its parts that change (the back arrow, the body, the actions), so focus and a
  // screen reader's place stay where they are; the lines that are new are read out, once.
  var every = +body.getAttribute("data-refresh") * 1000;
  if (every && window.fetch && window.DOMParser) {
    var said = document.createElement("div");
    said.className = "sr";
    said.setAttribute("role", "status");
    body.appendChild(said);
    var lines = function (scope) {
      return Array.prototype.slice.call(scope.querySelectorAll(".progress .tx, .checks .tx"));
    };
    var swap = function (fresh) {
      var known = lines(document).map(function (line) { return line.textContent; });
      ["main .fbar", "main .body", "main .actions"].forEach(function (selector) {
        var old = document.querySelector(selector), now = fresh.querySelector(selector);
        if (old && now && old.isEqualNode(now)) return;   // unchanged: kept, with a reader's place and focus in it
        if (now) now = document.importNode(now, true);
        if (old && now) old.replaceWith(now);
        else if (old) old.remove();
        else if (now) document.querySelector("main .body").after(now);     // the actions, once the work has ended
      });
      var news = lines(document).filter(function (line) { return known.indexOf(line.textContent) < 0; });
      if (!news.length) return;
      said.textContent = "";
      // a line and the note under it are read apart, each a sentence of its own: the line's whole text runs them
      // together, a version into the note's first word ("1.4.0Version")
      news.forEach(function (line) {
        Array.prototype.forEach.call(line.childNodes, function (part) {
          var p = document.createElement("p");
          p.textContent = part.textContent;
          said.appendChild(p);
        });
      });
    };
    var poll = function () {
      fetch(location.href).then(function (answer) {
        return answer.ok && !answer.redirected ? answer.text() : Promise.reject();
      }).then(function (text) {
        var fresh = new DOMParser().parseFromString(text, "text/html");
        if (!fresh.querySelector("main .body")) return Promise.reject();
        swap(fresh);
        if (fresh.body.hasAttribute("data-refresh")) setTimeout(poll, every);
      }).catch(function () { location.reload(); });
    };
    setTimeout(poll, every);
  } else if (every) {
    setTimeout(function () { location.reload(); }, every);
  }
})();
