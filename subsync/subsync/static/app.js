// AIOSubtitles public pages: the few moments that move. Every page works without this script; it only adds to what
// is already there. It loads nothing, sends nothing and stores nothing.
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function each(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  // The sync strip: your subtitles settle onto the file's own track with a spring, and the pointer can nudge them.
  each("[data-strip]", function (strip) {
    var slide = strip.querySelector(".slide");
    var nudge = strip.querySelector(".nudge");
    var head = strip.querySelector(".playhead");
    var lanes = strip.querySelector(".lanes");
    var rect = null;           // read once per hover or drag, not on every move
    var readout = strip.querySelector("[data-readout]");
    if (!slide || !nudge) return;
    var perPercent = parseFloat(strip.getAttribute("data-seconds")) || 0.35;
    var from = parseFloat(getComputedStyle(slide).getPropertyValue("--from")) || 0;
    var pos = still ? 0 : from, vel = 0, target = 0, frame = 0, last = 0, drag = null;
    var MAX = 9, STIFF = 150, DAMP = 15;
    slide.style.animation = "none";
    slide.style.transform = "none";
    if (readout) readout.hidden = false;

    function show() {
      nudge.style.transform = "translate3d(" + pos.toFixed(3) + "%,0,0)";
      if (readout) {
        var s = pos * perPercent;
        readout.textContent = Math.abs(s) < 0.05 ? "in sync" : (s > 0 ? "+" : "−") + Math.abs(s).toFixed(2) + " s";
      }
    }
    function step(now) {
      var dt = Math.min(0.032, (now - last) / 1000 || 0.016);
      last = now;
      vel += (STIFF * (target - pos) - DAMP * vel) * dt;
      pos += vel * dt;
      if (Math.abs(target - pos) < 0.005 && Math.abs(vel) < 0.01) {
        pos = target;
        frame = 0;
        show();
        return;
      }
      show();
      frame = requestAnimationFrame(step);
    }
    function go() {
      if (still) { pos = target; show(); return; }
      if (!frame) { last = performance.now(); frame = requestAnimationFrame(step); }
    }
    function place(clientX) {
      var r = rect || (rect = (lanes || strip).getBoundingClientRect());
      var x = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      if (head) head.style.transform = "translate3d(" + ((x - 0.41) * r.width).toFixed(1) + "px,0,0)";
      return { r: r, x: x };
    }
    function forget() { rect = null; }
    strip.addEventListener("pointerenter", forget);
    window.addEventListener("resize", forget);
    window.addEventListener("scroll", forget, { passive: true });
    show();
    if (!still) setTimeout(go, 450);

    strip.addEventListener("pointermove", function (e) {
      var p = place(e.clientX);
      if (drag !== null) target = Math.max(-MAX, Math.min(MAX, ((e.clientX - drag) / p.r.width) * 100));
      else if (e.pointerType === "mouse") target = (p.x - 0.5) * 2 * MAX * 0.6;
      else return;
      go();
    });
    strip.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse") return;
      drag = e.clientX;
      place(e.clientX);
    });
    function release() { drag = null; target = 0; go(); }
    strip.addEventListener("pointerup", release);
    strip.addEventListener("pointercancel", release);
    strip.addEventListener("pointerleave", release);
  });

  // Language chips: numbered in the order they were picked; the first is the main language. At most data-max.
  each("fieldset[data-max]", function (set) {
    var max = parseInt(set.getAttribute("data-max"), 10) || 4;
    var keep = set.querySelector("input[name=lang_order]");
    var count = set.querySelector("[data-count]");
    var boxes = Array.prototype.slice.call(set.querySelectorAll("input[type=checkbox]"));
    var order = (keep.value || "").split(",").filter(function (code) {
      return boxes.some(function (b) { return b.value === code && b.checked; });
    });
    boxes.forEach(function (b) { if (b.checked && order.indexOf(b.value) < 0) order.push(b.value); });

    function paint(changed) {
      keep.value = order.join(",");
      boxes.forEach(function (b) {
        var n = b.parentNode.querySelector(".chip-n");
        var at = order.indexOf(b.value);
        if (n) n.textContent = at < 0 ? "" : String(at + 1);
        b.disabled = at < 0 && order.length >= max;
        if (n && b === changed && at >= 0 && !still && n.animate) {
          n.animate([{ transform: "scale(.3)" }, { transform: "scale(1)" }],
                    { duration: 320, easing: "cubic-bezier(.3,1.6,.5,1)" });
        }
      });
      if (count) {
        count.textContent = count.getAttribute("data-template").replace("{n}", String(order.length));
        if (count.hidden) {          // the first count is shown quietly; changes are read out from then on
          count.hidden = false;
          count.setAttribute("aria-live", "polite");
        }
      }
    }
    boxes.forEach(function (b) {
      b.addEventListener("change", function () {
        var at = order.indexOf(b.value);
        if (b.checked && at < 0) order.push(b.value);
        if (!b.checked && at >= 0) order.splice(at, 1);
        paint(b);
      });
    });
    paint(null);
  });

  // Copy buttons: shown only where copying works.
  each("button[data-copy]", function (button) {
    var box = document.getElementById(button.getAttribute("data-copy"));
    var status = document.getElementById(button.getAttribute("data-copy") + "-status");
    if (!box || !navigator.clipboard || !window.isSecureContext) return;
    var label = button.textContent, timer = 0;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(box.value || box.textContent).then(function () {
        button.textContent = button.getAttribute("data-done");
        button.classList.add("done");
        if (status) status.textContent = button.getAttribute("data-done");
        clearTimeout(timer);
        timer = setTimeout(function () {
          button.textContent = label;
          button.classList.remove("done");
          if (status) status.textContent = "";
        }, 1800);
      }, function () {
        box.focus();
        box.select();
      });
    });
  });

  // Show or hide a password while typing it.
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

  // A problem after sending a form: bring it into view and to the screen reader.
  var problem = document.getElementById("problem");
  if (problem) problem.focus();

  // Parts below the first screen rise into place as they come into view, a little after one another.
  if (!still && "IntersectionObserver" in window) {
    var waiting = [];
    each(".rise", function (el) {
      if (el.getBoundingClientRect().top > window.innerHeight * 0.92) {
        el.classList.add("pending");
        waiting.push(el);
      }
    });
    var seen = new IntersectionObserver(function (entries) {
      var batch = 0;
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.style.setProperty("--d", (batch++ * 0.08).toFixed(2) + "s");
        entry.target.classList.remove("pending");
        seen.unobserve(entry.target);
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    waiting.forEach(function (el) { seen.observe(el); });
  }
})();
