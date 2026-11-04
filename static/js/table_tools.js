/* Adds filter, click-to-sort and CSV download to every <table data-table="name">.
   Optional attributes:
     data-filter="off"      no filter box (the page has its own search)
     data-csv-tip="..."     tooltip text for the CSV button
   On a <th>: data-nosort (cannot sort) and data-csv-skip (left out of the CSV). */
(function () {
  var SEARCH_ICON =
    '<svg class="h-4 w-4 opacity-60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';
  var DOWNLOAD_ICON =
    '<svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/></svg>';

  function dataRows(table) {
    var columns = table.tHead.rows[0].cells.length;
    return Array.prototype.filter.call(table.tBodies[0].rows, function (row) {
      return row.cells.length === columns; // skips the one-cell "nothing here" row
    });
  }

  function cellValue(row, index) {
    var cell = row.cells[index];
    return cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent.trim();
  }

  function isNumber(text) {
    return /^-?\d+([.,]\d+)?$/.test(text);
  }

  function compare(a, b) {
    if (isNumber(a) && isNumber(b)) {
      return parseFloat(a.replace(",", ".")) - parseFloat(b.replace(",", "."));
    }
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  function csvCell(text) {
    var value = text.replace(/\s*\n\s*/g, " / ").replace(/\s+/g, " ").trim();
    // Stop Excel from running a cell that starts like a formula (e.g. a name typed as =1+1).
    if (/^[=+\-@]/.test(value) && !/^[+-]?\d+([.,]\d+)?$/.test(value)) {
      value = "'" + value;
    }
    return '"' + value.replace(/"/g, '""') + '"';
  }

  function setup(table) {
    var state = { column: null, direction: 1, query: "" };
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);

    var toolbar = document.createElement("div");
    toolbar.className = "table-tools mb-2 flex flex-wrap items-center gap-2";
    var filterHtml =
      table.dataset.filter === "off"
        ? ""
        : '<label class="input input-sm w-full sm:w-72" title="Type to show only the rows that contain these letters">' +
          SEARCH_ICON +
          '<input type="search" placeholder="Filter rows" aria-label="Filter rows"></label>';
    toolbar.innerHTML =
      filterHtml +
      '<span class="table-count text-sm text-base-content/60" aria-live="polite"></span>' +
      '<span class="tooltip tooltip-left ml-auto"><button type="button" class="btn btn-sm btn-square btn-outline btn-primary" aria-label="Download CSV">' +
      DOWNLOAD_ICON +
      "</button></span>";
    toolbar
      .querySelector(".tooltip")
      .setAttribute(
        "data-tip",
        table.dataset.csvTip || "Download the rows shown as a CSV file (opens in Excel)"
      );
    var wrapper = table.parentElement;
    wrapper.parentNode.insertBefore(toolbar, wrapper);

    var countLabel = toolbar.querySelector(".table-count");

    function refresh() {
      var rows = dataRows(table);
      if (state.column !== null) {
        rows
          .slice()
          .sort(function (rowA, rowB) {
            return (
              state.direction *
              compare(cellValue(rowA, state.column), cellValue(rowB, state.column))
            );
          })
          .forEach(function (row) {
            table.tBodies[0].appendChild(row);
          });
      }
      var shown = 0;
      rows.forEach(function (row) {
        var match = !state.query || row.textContent.toLowerCase().indexOf(state.query) !== -1;
        row.hidden = !match;
        if (match) shown += 1;
      });
      if (!rows.length) {
        countLabel.textContent = "";
      } else if (state.query) {
        countLabel.textContent = shown
          ? "Showing " + shown + " of " + rows.length
          : "No rows match this filter";
      } else {
        countLabel.textContent = rows.length + (rows.length === 1 ? " row" : " rows");
      }
    }

    headers.forEach(function (th, index) {
      if (th.hasAttribute("data-nosort") || !th.textContent.trim()) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "sort-button";
      button.title = "Click to sort by " + th.textContent.trim();
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      th.setAttribute("aria-sort", "none");
      button.addEventListener("click", function () {
        state.direction = state.column === index ? -state.direction : 1;
        state.column = index;
        headers.forEach(function (other) {
          if (other.hasAttribute("aria-sort")) other.setAttribute("aria-sort", "none");
        });
        th.setAttribute("aria-sort", state.direction === 1 ? "ascending" : "descending");
        refresh();
      });
    });

    var filterInput = toolbar.querySelector("input");
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        state.query = filterInput.value.trim().toLowerCase();
        refresh();
      });
    }

    toolbar.querySelector("button").addEventListener("click", function () {
      var keep = headers
        .map(function (th, index) {
          return th.hasAttribute("data-csv-skip") || !th.textContent.trim() ? null : index;
        })
        .filter(function (index) {
          return index !== null;
        });
      var lines = [
        keep
          .map(function (index) {
            return csvCell(headers[index].innerText);
          })
          .join(","),
      ];
      dataRows(table)
        .filter(function (row) {
          return !row.hidden;
        })
        .forEach(function (row) {
          lines.push(
            keep
              .map(function (index) {
                return csvCell(row.cells[index].innerText);
              })
              .join(",")
          );
        });
      var blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
      var link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download =
        (table.dataset.table || "table") + "_" + new Date().toISOString().slice(0, 10) + ".csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    });

    table.addEventListener("tablerefresh", refresh);
    refresh();
  }

  function init() {
    document.querySelectorAll("table[data-table]").forEach(setup);
  }

  // A row replaced by HTMX (Activate / Deactivate, search) keeps the sort and filter.
  document.addEventListener("htmx:afterSwap", function () {
    document.querySelectorAll("table[data-table]").forEach(function (table) {
      table.dispatchEvent(new Event("tablerefresh"));
    });
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
