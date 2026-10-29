// DST audit review: Complete checkbox, assignee dropdown, the Notes modal and
// live updates.
// The Notes modal is a port of IMS's RA ("Resolving Actions") edit modal
// (IMS/js/index.js ensureRAEditModal/openRAEditModal): injected once, Save /
// ✕ / backdrop save and close, Cancel discards. Escape = Cancel is a DST addition.
// Live updates follow IMS's SSE client: one EventSource per page; teammates'
// changes are applied in place, and after a reconnect the page re-syncs from
// a fresh copy of itself so nothing missed while offline stays stale.
(function () {
  "use strict";
  var table = document.querySelector(".review-table[data-update-url]");
  if (!table) return;
  var URL_ = table.getAttribute("data-update-url");
  var EVENTS_URL = table.getAttribute("data-events-url");
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var CLIENT = Math.random().toString(36).slice(2) + Date.now().toString(36);
  var SNIPPET = 120;
  var live = false;

  function flash(el, ok) {
    if (!el) return;
    var cls = ok ? "highlight-success" : "highlight-error";
    el.classList.add(cls);
    setTimeout(function () { el.classList.remove(cls); }, ok ? 1000 : 2000);
  }

  function post(key, field, value, extra) {
    var body = { camera_key: key, field: field, value: value };
    if (extra) for (var k in extra) body[k] = extra[k];
    return fetch(URL_, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": CSRF,
        "X-Review-Client": CLIENT,
        Accept: "application/json",
      },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        data.status = r.status;
        if (!r.ok && !data.error) data.error = "Save failed (" + r.status + ").";
        return data;
      });
    }, function () {
      return { status: 0, error: "Network error; nothing was saved." };
    });
  }

  // A camera is one <tbody class="review-site" data-key> with a line per capture.
  var SITE = ".review-site[data-key]";

  function rowOf(el) { return el.closest(SITE); }

  function rowFor(key) {
    var rows = table.querySelectorAll(SITE);
    for (var i = 0; i < rows.length; i++) if (rows[i].getAttribute("data-key") === key) return rows[i];
    return null;
  }

  // ── Render one camera's state (own saves, live events and re-syncs) ──
  function renderNotes(cell, text) {
    cell.setAttribute("data-notes-value", text);
    cell.textContent = "";
    var span = document.createElement("span");
    if (text) {
      span.className = "review-notes-text";
      span.textContent = text.length > SNIPPET ? text.slice(0, SNIPPET - 1) + "…" : text;
    } else {
      span.className = "text-dim";
      span.textContent = "Click to add notes…";
    }
    cell.appendChild(span);
  }

  function setComplete(row, completed, by) {
    var box = row.querySelector(".review-complete-box");
    if (box) box.checked = completed;
    row.classList.toggle("is-complete", completed);
    var label = row.querySelector(".review-complete-by");
    if (label) label.textContent = completed ? by : "";
  }

  function setAssignee(row, name) {
    var sel = row.querySelector(".review-assign");
    if (!sel || sel.disabled || document.activeElement === sel) return false;
    if (name && !sel.querySelector('option[value="' + CSS.escape(name) + '"]')) {
      var opt = document.createElement("option");
      opt.value = opt.textContent = name;
      sel.appendChild(opt);
    }
    sel.value = name;
    sel.setAttribute("data-prev-value", name);
    return true;
  }

  // Returns the cells that changed, for the flash.
  function applyState(row, s, by) {
    var changed = [];
    var box = row.querySelector(".review-complete-box");
    if (box && !box.disabled) {  // disabled = our own save is in flight
      if (box.checked !== s.completed) changed.push(box.closest("td"));
      setComplete(row, s.completed, s.completed_by);
    }
    var sel = row.querySelector(".review-assign");
    if (sel && sel.value !== s.assigned_to && setAssignee(row, s.assigned_to)) changed.push(sel);
    var cell = row.querySelector(".review-notes");
    if (cell && (cell.getAttribute("data-notes-value") || "") !== s.notes) {
      if (cell === activeCell) {
        // Don't swap the text under the editor: the save's conflict check
        // still compares against what it opened with. Just say so.
        showNotice((by || "Someone") + " just changed these notes. Saving will ask before replacing theirs.");
      } else {
        renderNotes(cell, s.notes);
        changed.push(cell);
      }
    }
    return changed;
  }

  function applyProgress(p) {
    var n = document.getElementById("review-complete-n");
    if (n) n.textContent = p.complete + "/" + n.textContent.split("/")[1];
    document.querySelectorAll(".review-progress-chip[data-tech]").forEach(function (chip) {
      var t = p.techs[chip.getAttribute("data-tech")] || { total: 0, complete: 0 };
      chip.querySelector(".review-progress-n").textContent = t.complete + "/" + t.total;
    });
    var un = document.getElementById("review-unassigned");
    if (un) {
      un.querySelector(".review-unassigned-n").textContent = p.unassigned;
      un.hidden = !p.unassigned;
    }
  }

  // Without a live connection, keep the header count right for our own saves.
  function bumpCompleteCount(delta) {
    if (live) return;
    var n = document.getElementById("review-complete-n");
    if (!n) return;
    var parts = n.textContent.split("/");
    n.textContent = (parseInt(parts[0], 10) + delta) + "/" + parts[1];
  }

  // ── Complete / assignee ──
  table.addEventListener("change", function (e) {
    var box = e.target.closest(".review-complete-box");
    if (box) {
      var row = rowOf(box), want = box.checked;
      box.disabled = true;
      post(row.getAttribute("data-key"), "completed", want).then(function (res) {
        box.disabled = false;
        if (res.error) {
          box.checked = !want;
          flash(box.closest("td"), false);
          alert(res.error);
          return;
        }
        setComplete(row, res.state.completed, res.state.completed_by);
        if (res.state.completed === want) bumpCompleteCount(want ? 1 : -1);
        flash(box.closest("td"), true);
      });
      return;
    }
    var sel = e.target.closest(".review-assign");
    if (sel) {
      var prev = sel.getAttribute("data-prev-value") || "";
      var r = rowOf(sel);
      sel.disabled = true;
      post(r.getAttribute("data-key"), "assigned_to", sel.value).then(function (res) {
        sel.disabled = false;
        if (res.error) {
          sel.value = prev;
          flash(sel, false);
          alert(res.error);
          return;
        }
        setAssignee(r, res.state.assigned_to);
        flash(sel, true);
      });
    }
  });

  // ── Notes modal (IMS RA modal port) ──
  var activeCell = null;

  function ensureNotesModal() {
    var existing = document.getElementById("dst-notes-modal");
    if (existing) return existing;
    var modal = document.createElement("div");
    modal.id = "dst-notes-modal";
    modal.className = "dst-notes-modal-overlay";
    modal.innerHTML =
      '<div class="dst-notes-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="dst-notes-title">' +
        '<div class="dst-notes-modal-header">' +
          '<h3 id="dst-notes-title" class="dst-notes-modal-title">Notes</h3>' +
          '<button id="dst-notes-close" class="dst-notes-modal-close" type="button" aria-label="Save and close">✕</button>' +
        '</div>' +
        '<textarea id="dst-notes-text" rows="6" maxlength="4000"></textarea>' +
        '<div id="dst-notes-notice" class="dst-notes-notice" hidden></div>' +
        '<div id="dst-notes-error" class="dst-notes-error" hidden></div>' +
        '<div class="dst-notes-modal-footer">' +
          '<button id="dst-notes-cancel" class="btn-secondary" type="button">Cancel</button>' +
          '<button id="dst-notes-save" class="btn-secondary dst-notes-save-btn" type="button">Save</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
    document.getElementById("dst-notes-close").onclick = saveAndCloseNotesModal;
    document.getElementById("dst-notes-cancel").onclick = closeNotesModal;
    document.getElementById("dst-notes-save").onclick = saveAndCloseNotesModal;
    modal.onclick = function (e) { if (e.target === modal) saveAndCloseNotesModal(); };
    modal.addEventListener("keydown", function (e) { if (e.key === "Escape") closeNotesModal(); });
    return modal;
  }

  function showError(msg) {
    var err = document.getElementById("dst-notes-error");
    err.textContent = msg || "";
    err.hidden = !msg;
  }

  function showNotice(msg) {
    var el = document.getElementById("dst-notes-notice");
    if (!el) return;
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  function openNotesModal(cell) {
    var modal = ensureNotesModal();
    activeCell = cell;
    var row = rowOf(cell);
    document.getElementById("dst-notes-title").textContent = "Notes — " + (row.getAttribute("data-label") || row.getAttribute("data-key"));
    var text = document.getElementById("dst-notes-text");
    text.value = cell.getAttribute("data-notes-value") || "";
    showError("");
    showNotice("");
    modal.classList.add("is-open");
    setTimeout(function () { text.focus(); text.setSelectionRange(text.value.length, text.value.length); }, 50);
  }

  function closeNotesModal() {
    var modal = document.getElementById("dst-notes-modal");
    if (modal) modal.classList.remove("is-open");
    if (activeCell) activeCell.focus();
    activeCell = null;
  }

  function saveAndCloseNotesModal() {
    var cell = activeCell;
    if (!cell) return closeNotesModal();
    var value = document.getElementById("dst-notes-text").value;
    var prev = cell.getAttribute("data-notes-value") || "";
    if (value === prev) return closeNotesModal();
    var saveBtn = document.getElementById("dst-notes-save");
    saveBtn.disabled = true;
    post(rowOf(cell).getAttribute("data-key"), "notes", value, { prev: prev }).then(function (res) {
      saveBtn.disabled = false;
      if (res.error) {
        // Keep the modal and the typed text so nothing is lost.
        var msg = res.error;
        if (res.status === 409 && res.state) {
          renderNotes(cell, res.state.notes);
          msg += "\n\nTheir notes:\n" + (res.state.notes || "(empty)");
        }
        showNotice("");
        showError(msg);
        flash(cell, false);
        return;
      }
      renderNotes(cell, res.state.notes);
      closeNotesModal();
      flash(cell, true);
    });
  }

  table.addEventListener("click", function (e) {
    var cell = e.target.closest(".review-notes");
    if (cell) openNotesModal(cell);
  });
  table.addEventListener("keydown", function (e) {
    var cell = e.target.closest(".review-notes");
    if (cell && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      openNotesModal(cell);
    }
  });

  // ── Live updates (SSE) ──
  var badge = document.getElementById("review-live");

  function setLive(on, text) {
    live = on;
    if (!badge) return;
    badge.textContent = text;
    badge.classList.toggle("is-on", on);
    badge.classList.toggle("is-off", !on);
  }

  // Catch up on anything missed while disconnected: fetch this page again
  // and apply each visible row's state from it.
  function resync() {
    fetch(window.location.href, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        doc.querySelectorAll(SITE).forEach(function (fresh) {
          var row = rowFor(fresh.getAttribute("data-key"));
          if (!row) return;
          var box = fresh.querySelector(".review-complete-box");
          var sel = fresh.querySelector(".review-assign");
          var cell = fresh.querySelector(".review-notes");
          applyState(row, {
            completed: !!(box && box.checked),
            completed_by: (fresh.querySelector(".review-complete-by") || {}).textContent || "",
            assigned_to: sel ? sel.value : "",
            notes: cell ? cell.getAttribute("data-notes-value") || "" : "",
          }, "");
        });
        ["review-complete-n"].forEach(function (id) {
          var a = document.getElementById(id), b = doc.getElementById(id);
          if (a && b) a.textContent = b.textContent;
        });
        doc.querySelectorAll(".review-progress-chip[data-tech]").forEach(function (b) {
          var a = document.querySelector('.review-progress-chip[data-tech="' + CSS.escape(b.getAttribute("data-tech")) + '"] .review-progress-n');
          if (a) a.textContent = b.querySelector(".review-progress-n").textContent;
        });
      })
      .catch(function () { /* the next reconnect tries again */ });
  }

  if (EVENTS_URL && window.EventSource) {
    var connectedOnce = false;
    var source = new EventSource(EVENTS_URL);
    source.addEventListener("connected", function () {
      setLive(true, "Live updates: on");
      if (connectedOnce) resync();
      connectedOnce = true;
    });
    source.addEventListener("review", function (e) {
      var data;
      try { data = JSON.parse(e.data); } catch (err) { return; }
      applyProgress(data.progress);
      if (data.client === CLIENT) return; // our own save, already shown
      var row = rowFor(data.state.camera_key);
      if (!row) return; // filtered out, or new since this page loaded
      applyState(row, data.state, data.by).forEach(function (el) { flash(el, true); });
    });
    source.onerror = function () {
      if (source.readyState !== EventSource.CLOSED) setLive(false, "Live updates: reconnecting…");
      else setLive(false, "Live updates: off (refresh to retry)");
    };
    window.addEventListener("pagehide", function () { source.close(); });
  } else if (badge) {
    badge.hidden = true;
  }
})();
