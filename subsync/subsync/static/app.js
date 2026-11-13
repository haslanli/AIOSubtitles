// AIOSubtitles' pages: the few moments that move. Every page works without this script: the sign-up then opens by its
// link and shows its questions one under another. With it, the sign-up is a layer that slides in, one question per
// screen, Back or Escape reversing it; the language slots show one place and "Add language" up to the instance's
// limit; passwords can be shown; the personal link can be copied. It loads nothing, sends nothing and stores nothing.
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

    var paint = function (back, focusProblem) {
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
      // the screen's field, or its heading when it asks with choices (as the approved pieces do)
      var target = (focusProblem && problem) || screen.querySelector("input.input, h2.q");
      if (target && target.classList.contains("q")) target.tabIndex = -1;
      if (target) later(function () { target.focus({ preventScroll: true }); });
    };
    var open = function (from, name, focusProblem) {
      opener = from || opener;
      var found = screens.map(function (s) { return s.getAttribute("data-screen"); }).indexOf(name);
      at = found < 0 ? 0 : found;
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
      else if (t.hasAttribute("data-back") && layer.contains(t)) {
        e.preventDefault();
        if (at === 0) close(); else { at -= 1; paint(true); }
      } else if (t.hasAttribute("data-next")) { at = Math.min(at + 1, screens.length - 1); paint(false); }
    });
    form.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || !e.target.matches("input.input") || at === screens.length - 1) return;
      e.preventDefault();
      at += 1;
      paint(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && body.classList.contains("open")) close();
    });
    // after a problem the page comes back with the layer open at the screen it is about; a link to #signup opens it
    var openAt = body.getAttribute("data-open-at");
    if (openAt) open(null, openAt, true);
    else if (location.hash === "#signup") {
      history.replaceState(null, "", location.pathname + location.search);
      open(document.querySelector(".go [data-open]"));
    } else {
      layer.setAttribute("aria-hidden", "true");
      screens.forEach(function (s, i) { s.hidden = i !== 0; });
    }
    // a link in the problem summary goes to the screen with its field
    each("#problem a[href^='#']", function (a) {
      a.addEventListener("click", function (e) {
        var field = document.getElementById(a.getAttribute("href").slice(1));
        var screen = field && field.closest("[data-screen]");
        if (!screen) return;
        e.preventDefault();
        at = screens.indexOf(screen);
        paint(false);
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
    var max = parseInt(group.getAttribute("data-max-languages"), 10) || 4;
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
          remove.setAttribute("aria-label", "Remove: " + nameOf(select));
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
})();
