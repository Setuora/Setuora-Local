(function () {
  "use strict";

  var MINIMUM_RECORDS = 10;
  var DEFAULT_ROW_HEIGHT = 44;
  var WRAPPER_SELECTOR = ".table-scroll, .access-table-scroll";
  var EMPTY_RECORD_CLASS = "table-empty-record";
  var scheduled = false;

  function isVisible(row) {
    return !row.hidden && window.getComputedStyle(row).display !== "none";
  }

  function isEmptyState(row) {
    return Array.prototype.some.call(row.cells, function (cell) {
      return cell.classList.contains("empty");
    });
  }

  function rowHeight(row) {
    return row.getBoundingClientRect().height;
  }

  function columnCount(table) {
    var headerRows = table.tHead ? table.tHead.rows : [];
    var referenceRow = headerRows.length
      ? headerRows[headerRows.length - 1]
      : table.querySelector("tbody > tr:not(." + EMPTY_RECORD_CLASS + ")");
    if (!referenceRow) return 1;
    return Array.prototype.reduce.call(referenceRow.cells, function (count, cell) {
      return count + Math.max(1, cell.colSpan || 1);
    }, 0) || 1;
  }

  function createEmptyRecord(table) {
    var row = document.createElement("tr");
    var cell = document.createElement("td");
    row.className = EMPTY_RECORD_CLASS;
    row.setAttribute("aria-hidden", "true");
    row.setAttribute("role", "presentation");
    cell.colSpan = columnCount(table);
    cell.textContent = "\u00a0";
    row.appendChild(cell);
    return row;
  }

  function syncEmptyRecords(table, rows, fallbackHeight) {
    var bodies = Array.prototype.slice.call(table.tBodies);
    if (!bodies.length) return [];

    var occupiedRows = rows.filter(function (row) {
      return !row.classList.contains(EMPTY_RECORD_CLASS);
    });
    var required = Math.max(0, MINIMUM_RECORDS - occupiedRows.length);
    var emptyRecords = Array.prototype.slice.call(
      table.querySelectorAll("tbody > tr." + EMPTY_RECORD_CLASS)
    );

    while (emptyRecords.length > required) {
      emptyRecords.pop().remove();
    }
    while (emptyRecords.length < required) {
      var emptyRecord = createEmptyRecord(table);
      bodies[bodies.length - 1].appendChild(emptyRecord);
      emptyRecords.push(emptyRecord);
    }

    table.closest(WRAPPER_SELECTOR).style.setProperty(
      "--table-empty-record-height",
      Math.ceil(fallbackHeight) + "px"
    );
    return emptyRecords;
  }

  function median(values) {
    if (!values.length) return DEFAULT_ROW_HEIGHT;
    var sorted = values.slice().sort(function (left, right) {
      return left - right;
    });
    var middle = Math.floor(sorted.length / 2);
    if (sorted.length % 2) return sorted[middle];
    return (sorted[middle - 1] + sorted[middle]) / 2;
  }

  function measureTable(table) {
    var wrapper = table.closest(WRAPPER_SELECTOR);
    if (!wrapper) return;

    var visibleRows = Array.prototype.filter.call(
      table.querySelectorAll("tbody > tr"),
      isVisible
    );
    var recordRows = visibleRows.filter(function (row) {
      return !isEmptyState(row) && !row.classList.contains(EMPTY_RECORD_CLASS);
    });
    var displayedRows = recordRows.slice(0, MINIMUM_RECORDS);
    var displayedHeights = displayedRows.map(rowHeight).filter(function (height) {
      return height > 0;
    });
    var fallbackHeight = median(displayedHeights);
    var emptyRecords = syncEmptyRecords(table, visibleRows, fallbackHeight);
    var viewportRows = visibleRows
      .filter(function (row) {
        return !row.classList.contains(EMPTY_RECORD_CLASS);
      })
      .concat(emptyRecords)
      .slice(0, MINIMUM_RECORDS);
    var viewportHeights = viewportRows.map(rowHeight).filter(function (height) {
      return height > 0;
    });
    var bodyHeight = viewportHeights.length
      ? viewportHeights.reduce(function (total, height) {
          return total + height;
        }, 0)
      : fallbackHeight * MINIMUM_RECORDS;

    var caption = table.caption ? table.caption.getBoundingClientRect().height : 0;
    var header = table.tHead ? table.tHead.getBoundingClientRect().height : 0;
    var horizontalScrollbar = table.scrollWidth > wrapper.clientWidth
      ? Math.max(0, wrapper.offsetHeight - wrapper.clientHeight)
      : 0;
    var viewportHeight = Math.ceil(caption + header + bodyHeight + horizontalScrollbar);

    wrapper.classList.add("table-record-viewport");
    wrapper.style.setProperty("--table-record-viewport-height", viewportHeight + "px");
  }

  function updateTableViewports() {
    scheduled = false;
    document.querySelectorAll("table").forEach(measureTable);
  }

  function scheduleUpdate() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(updateTableViewports);
  }

  scheduleUpdate();
  window.addEventListener("resize", scheduleUpdate);

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(scheduleUpdate);
  }

  new MutationObserver(scheduleUpdate).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["hidden", "open"],
  });
})();
