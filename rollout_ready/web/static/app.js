/* Small enhancements only. Every form works without this file. */
(function () {
  "use strict";

  // Phones: collapse the comparison form to a summary line with a Change button.
  document.querySelectorAll("[data-collapse-on-phone]").forEach(function (el) {
    if (window.matchMedia("(max-width: 760px)").matches) el.removeAttribute("open");
  });

  // Changing the prompt on See results or Test a change reloads with that prompt's defaults.
  document.querySelectorAll("[data-autosubmit-suite]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      window.location.href = "/results?suite=" + encodeURIComponent(sel.value);
    });
  });
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.name === "suite") {
        window.location.href = "/test?suite=" + encodeURIComponent(sel.value);
      } else {
        sel.form.submit();
      }
    });
  });

  // Copy summary to share.
  document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
    var source = document.getElementById(btn.getAttribute("data-copy-target"));
    var status = btn.parentElement.querySelector(".copy-status");
    if (!source || !navigator.clipboard) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(source.textContent).then(function () {
        if (status) status.textContent = "Copied. Paste it into Slack or an email.";
      }, function () {
        if (status) status.textContent = "Could not copy. Use Download summary instead.";
      });
    });
  });

  // "I disagree with the AI reviewer": only ask how the example should count for "Something else".
  document.querySelectorAll("form[data-disagree]").forEach(function (form) {
    var box = form.querySelector("[data-count-as]");
    if (!box) return;
    var label = box.querySelector("label");
    if (label) label.textContent = "How should this example count?";
    var sync = function () {
      var picked = form.querySelector("input[name=call]:checked");
      box.hidden = !(picked && picked.value === "other");
    };
    form.querySelectorAll("input[name=call]").forEach(function (r) { r.addEventListener("change", sync); });
    sync();
  });

  // Add an example without a page reload.
  document.querySelectorAll("form[data-add-case]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.fetch || !window.FormData) return;
      ev.preventDefault();
      var status = form.querySelector("[data-add-status]");
      status.textContent = "Saving...";
      fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) { status.textContent = data.error; return; }
          document.querySelectorAll("[data-case-list]").forEach(function (list) {
            var li = document.createElement("li");
            var strong = document.createElement("strong");
            strong.textContent = data["case"].name;
            li.appendChild(strong);
            var tag = document.createElement("span");
            tag.className = "tag tag-kind tag-" + data["case"].kind;
            tag.textContent = data.kind_label;
            li.appendChild(tag);
            if (data["case"].label) {
              var span = document.createElement("span");
              span.className = "muted";
              span.textContent = " " + data["case"].label;
              li.appendChild(span);
            }
            list.appendChild(li);
          });
          document.querySelectorAll("[data-case-count]").forEach(function (el) { el.textContent = data.count; });
          if (!data.nudge) document.querySelectorAll("[data-nudge]").forEach(function (el) { el.hidden = true; });
          var est = document.querySelector("[data-estimate]");
          if (est && data.estimate) est.textContent = data.estimate;
          form.reset();
          status.textContent = "Saved “" + data["case"].name + "”. It now has " + data.count + " examples.";
        })
        .catch(function () { form.submit(); });
    });
  });

  // Show that a run has started.
  document.querySelectorAll("form[data-run-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = document.querySelector("[data-run-button]");
      var status = document.querySelector("[data-run-status]");
      if (btn) btn.setAttribute("aria-disabled", "true");
      if (status) status.textContent = "Starting the test...";
    });
  });

  // Progress page: poll for updates.
  var job = document.querySelector("[data-job]");
  if (job) {
    var id = job.getAttribute("data-job");
    var bar = document.getElementById("job-progress");
    var text = job.querySelector("[data-job-status]");
    var tick = function () {
      fetch("/jobs/" + id + "/status").then(function (r) { return r.json(); }).then(function (s) {
        if (s.error) { window.location.reload(); return; }
        if (s.finished) { window.location.href = s.done_url; return; }
        if (bar) bar.value = s.percent;
        if (text) text.textContent = s.message + " (" + s.done + " of " + (s.total || "?") + " calls)";
        setTimeout(tick, 1500);
      }).catch(function () { setTimeout(tick, 3000); });
    };
    setTimeout(tick, 1000);
  }
})();
