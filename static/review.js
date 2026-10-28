// DST audit review: Complete checkbox, assignee dropdown and the Notes modal.
// The Notes modal is a port of IMS's RA ("Resolving Actions") edit modal
// (IMS/js/index.js ensureRAEditModal/openRAEditModal): injected once, Save /
// ✕ / backdrop save and close, Cancel discards. Escape = Cancel is a DST addition.
(function () {
  "use strict";
  var table = document.querySelector(".review-table[data-update-url]");
  if (!table) return;
  var URL_ = table.getAttribute("data-update-url");
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var SNIPPET = 120;

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
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF, Accept: "application/json" },
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

  function rowOf(el) { return el.closest("tr[data-key]"); }

  // ── Complete ──
  function updateCompleteCount(delta) {
    var n = document.getElementById("review-complete-n");
    if (!n) return;
    var parts = n.textContent.split("/");
    n.textContent = (parseInt(parts[0], 10) + delta) + "/" + parts[1];
  }

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
        var s = res.state;
        box.checked = s.completed;
        row.classList.toggle("is-complete", s.completed);
        row.querySelector(".review-complete-by").textContent = s.completed ? s.completed_by : "";
        if (s.completed === want) updateCompleteCount(want ? 1 : -1);
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
        sel.value = res.state.assigned_to;
        sel.setAttribute("data-prev-value", res.state.assigned_to);
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

  function showError(msg) {
    var err = document.getElementById("dst-notes-error");
    err.textContent = msg || "";
    err.hidden = !msg;
  }

  function openNotesModal(cell) {
    var modal = ensureNotesModal();
    activeCell = cell;
    var row = rowOf(cell);
    document.getElementById("dst-notes-title").textContent = "Notes — " + (row.getAttribute("data-label") || row.getAttribute("data-key"));
    var text = document.getElementById("dst-notes-text");
    text.value = cell.getAttribute("data-notes-value") || "";
    showError("");
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
})();
