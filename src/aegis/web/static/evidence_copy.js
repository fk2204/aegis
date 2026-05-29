// evidence_copy.js — copy-to-clipboard for evidence panel rows.
//
// Wires up every ``.evidence-copy-btn`` to capture the rows of every
// ``<table class="ledger">`` inside the button's enclosing ``<details>``
// and write them to the clipboard as tab-separated text:
//
//   YYYY-MM-DD<TAB>Description<TAB>Amount<TAB>p7 L14
//
// Workers paste into Google Sheets / email drafts / Close notes /
// bank-portal search boxes. Plain text only — no HTML, no CSS carry-
// over.
//
// Lightweight progressive enhancement. If the Clipboard API isn't
// available (older browsers, file:// origin, permission denied), the
// button flashes a "(unsupported)" notice and logs to console. The
// underlying table rows still select cleanly with cursor as a manual
// fallback — no ``user-select: none`` anywhere in aegis-tool.css.
//
// IIFE so the helpers stay out of window scope.

(function () {
  "use strict";

  function gatherRowsFromDetails(detailsEl) {
    const lines = [];
    const tables = detailsEl.querySelectorAll("table.ledger");
    for (const table of tables) {
      for (const row of table.querySelectorAll("tr")) {
        const cells = row.querySelectorAll("td");
        if (cells.length === 0) continue; // header row or empty
        const parts = [];
        for (const cell of cells) {
          // Collapse internal whitespace so a multi-line description
          // (wrapped <br> or runaway spacing) lands on one TSV cell.
          parts.push(cell.textContent.trim().replace(/\s+/g, " "));
        }
        lines.push(parts.join("\t"));
      }
    }
    return lines.join("\n");
  }

  function flashButton(btn, text) {
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    btn.textContent = text;
    btn.classList.add("evidence-copy-flash");
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove("evidence-copy-flash");
    }, 1500);
  }

  function onClick(event) {
    const btn = event.currentTarget;
    const details = btn.closest("details");
    if (!details) {
      console.warn("evidence_copy: button has no enclosing <details>");
      return;
    }
    const text = gatherRowsFromDetails(details);
    if (!text) {
      flashButton(btn, "(no rows)");
      return;
    }
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      console.warn("evidence_copy: Clipboard API unavailable");
      flashButton(btn, "(unsupported)");
      return;
    }
    navigator.clipboard.writeText(text).then(
      function () {
        flashButton(btn, "Copied!");
      },
      function (err) {
        console.warn("evidence_copy: writeText failed", err);
        flashButton(btn, "(failed)");
      }
    );
  }

  function wire() {
    const buttons = document.querySelectorAll(".evidence-copy-btn");
    for (const btn of buttons) {
      btn.addEventListener("click", onClick);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
