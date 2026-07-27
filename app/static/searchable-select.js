(function () {
  const selectQuery =
    'select:not([multiple]):not([data-access-picker]):not([data-searchable-select="off"])';
  const controls = new Map();
  let openControl = null;
  let controlId = 0;

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function optionText(option) {
    return (option.label || option.textContent || "").trim();
  }

  function normalizeSearch(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim()
      .replace(/\s+/g, " ");
  }

  function tokenize(value) {
    return normalizeSearch(value).split(" ").filter(Boolean);
  }

  function compact(value) {
    return normalizeSearch(value).replace(/\s+/g, "");
  }

  function isSubsequence(needle, haystack) {
    if (!needle) return true;
    let index = 0;
    for (const char of haystack) {
      if (char === needle[index]) index += 1;
      if (index === needle.length) return true;
    }
    return false;
  }

  function editSimilarity(a, b) {
    if (!a && !b) return 1;
    if (!a || !b) return 0;
    const previous = Array.from({ length: b.length + 1 }, (_, index) => index);
    const current = new Array(b.length + 1);
    for (let i = 1; i <= a.length; i += 1) {
      current[0] = i;
      for (let j = 1; j <= b.length; j += 1) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        current[j] = Math.min(
          current[j - 1] + 1,
          previous[j] + 1,
          previous[j - 1] + cost,
        );
      }
      previous.splice(0, previous.length, ...current);
    }
    return 1 - previous[b.length] / Math.max(a.length, b.length);
  }

  function gramSimilarity(a, b) {
    if (a.length < 2 || b.length < 2) return editSimilarity(a, b);
    const grams = (value) => {
      const result = new Map();
      for (let index = 0; index < value.length - 1; index += 1) {
        const gram = value.slice(index, index + 2);
        result.set(gram, (result.get(gram) || 0) + 1);
      }
      return result;
    };
    const left = grams(a);
    const right = grams(b);
    let overlap = 0;
    left.forEach((count, gram) => {
      overlap += Math.min(count, right.get(gram) || 0);
    });
    return (2 * overlap) / (a.length + b.length - 2);
  }

  function tokenSimilarity(queryToken, candidateToken) {
    if (!queryToken || !candidateToken) return 0;
    if (candidateToken === queryToken) return 1;
    if (candidateToken.startsWith(queryToken)) return 0.94;
    if (candidateToken.includes(queryToken)) return 0.88;
    if (queryToken.length >= 3 && isSubsequence(queryToken, candidateToken)) {
      return Math.max(0.68, queryToken.length / candidateToken.length - 0.04);
    }
    return Math.max(
      editSimilarity(queryToken, candidateToken),
      gramSimilarity(queryToken, candidateToken),
    );
  }

  function fuzzyScore(query, record) {
    const normalizedQuery = normalizeSearch(query);
    if (!normalizedQuery) return 1;
    const compactQuery = compact(normalizedQuery);
    if (!compactQuery) return 1;

    if (record.searchText.includes(normalizedQuery)) return 1;
    if (record.compactText.includes(compactQuery)) return 0.98;
    if (record.value.includes(compactQuery)) return 0.96;

    const queryTokens = tokenize(normalizedQuery);
    if (!queryTokens.length) return 0;
    const tokenScores = queryTokens.map((queryToken) =>
      record.tokens.reduce(
        (best, token) => Math.max(best, tokenSimilarity(queryToken, token)),
        0,
      ),
    );
    const average =
      tokenScores.reduce((total, score) => total + score, 0) / tokenScores.length;
    const weakest = Math.min(...tokenScores);
    const phraseScore = Math.max(
      editSimilarity(compactQuery, record.compactText.slice(0, compactQuery.length)),
      gramSimilarity(compactQuery, record.compactText),
    );

    return Math.max(average * 0.82 + weakest * 0.18, phraseScore * 0.72);
  }

  function optionSearchText(option) {
    return normalizeSearch([
      optionText(option),
      option.value || "",
      option.dataset.search || "",
      option.parentElement?.label || "",
    ].join(" "));
  }

  function isOptionVisible(option) {
    const group = option.parentElement;
    return !option.hidden && !(group?.tagName === "OPTGROUP" && group.hidden);
  }

  function isOptionDisabled(option) {
    const group = option.parentElement;
    return Boolean(
      option.disabled ||
        (group?.tagName === "OPTGROUP" && group.disabled) ||
        option.hidden ||
        group?.hidden,
    );
  }

  function syncButton(control) {
    const { button, select, value } = control;
    const selected = select.selectedOptions[0];
    value.textContent = selected ? optionText(selected) : "Select";
    button.disabled = select.disabled;
    button.setAttribute("aria-disabled", select.disabled ? "true" : "false");
    button.setAttribute("aria-required", select.required ? "true" : "false");
    control.wrapper.classList.toggle("is-disabled", select.disabled);
    if (select.disabled && openControl === control) closeControl(control);
  }

  function syncSelectedOption(control) {
    control.items.forEach((item) => {
      const selected = Number(item.dataset.index) === control.select.selectedIndex;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-selected", selected ? "true" : "false");
    });
  }

  function filterOptions(control) {
    const query = control.search.value.trim();
    const normalizedQuery = normalizeSearch(query);
    const threshold = normalizedQuery.length < 3 ? 0.88 : 0.58;
    let visibleCount = 0;
    let firstEnabled = null;
    const ranked = [];
    const visibleGroups = new Set();

    control.records.forEach((record) => {
      const score = fuzzyScore(query, record);
      const matches = !normalizedQuery || score >= threshold;
      const visible = matches && isOptionVisible(record.option);
      record.item.hidden = !visible;
      record.item.style.order =
        visible && normalizedQuery && !control.hasGroups
          ? String(Math.round((1 - score) * 1000))
          : "";
      if (visible) visibleCount += 1;
      if (visible && !firstEnabled) firstEnabled = record.item;
      if (visible) {
        ranked.push(record);
        if (record.group) visibleGroups.add(record.group);
      }
    });

    control.groupLabels.forEach((label) => {
      label.hidden = !visibleGroups.has(label.dataset.group);
    });

    if (normalizedQuery && !control.hasGroups) {
      ranked
        .sort((a, b) => {
          const left = Number(a.item.style.order || 0);
          const right = Number(b.item.style.order || 0);
          return left - right || a.index - b.index;
        })
        .forEach((record) => control.options.insertBefore(record.item, control.empty));
    } else if (!normalizedQuery && !control.hasGroups) {
      control.records.forEach((record) =>
        control.options.insertBefore(record.item, control.empty),
      );
    }

    control.empty.hidden = visibleCount > 0;
    syncSelectedOption(control);
    return firstEnabled;
  }

  function selectOption(control, index) {
    const option = control.select.options[index];
    if (!option || isOptionDisabled(option)) return;
    control.select.selectedIndex = index;
    control.select.dispatchEvent(new Event("input", { bubbles: true }));
    control.select.dispatchEvent(new Event("change", { bubbles: true }));
    control.lastValue = control.select.value;
    syncButton(control);
    syncSelectedOption(control);
    closeControl(control);
    control.button.focus();
  }

  function focusVisibleItem(control, direction) {
    const visibleItems = control.items.filter((item) => !item.hidden);
    if (!visibleItems.length) return;
    const currentIndex = visibleItems.indexOf(document.activeElement);
    const nextIndex =
      currentIndex === -1
        ? direction > 0
          ? 0
          : visibleItems.length - 1
        : (currentIndex + direction + visibleItems.length) % visibleItems.length;
    visibleItems[nextIndex].focus({ preventScroll: true });
  }

  function renderOptions(control) {
    const { options, select } = control;
    options.replaceChildren();
    control.records = [];
    control.items = [];
    control.groupLabels = [];
    control.hasGroups = false;
    let lastGroup = "";

    Array.from(select.options).forEach((option, index) => {
      const group =
        option.parentElement?.tagName === "OPTGROUP"
          ? option.parentElement.label || ""
          : "";
      if (group && group !== lastGroup) {
        control.hasGroups = true;
        const groupLabel = document.createElement("span");
        groupLabel.className = "searchable-select__group";
        groupLabel.dataset.group = group;
        groupLabel.textContent = group;
        options.append(groupLabel);
        control.groupLabels.push(groupLabel);
        lastGroup = group;
      }

      const item = document.createElement("button");
      item.type = "button";
      item.className = "searchable-select__option";
      item.dataset.index = String(index);
      item.dataset.value = option.value;
      item.setAttribute("role", "option");
      item.textContent = optionText(option);
      item.disabled = isOptionDisabled(option);
      item.addEventListener("click", () => selectOption(control, index));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          closeControl(control);
          control.button.focus();
        } else if (event.key === "ArrowDown") {
          event.preventDefault();
          focusVisibleItem(control, 1);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          focusVisibleItem(control, -1);
        } else if (event.key === "Home") {
          event.preventDefault();
          control.items.find((candidate) => !candidate.hidden)?.focus({
            preventScroll: true,
          });
        } else if (event.key === "End") {
          event.preventDefault();
          [...control.items]
            .reverse()
            .find((candidate) => !candidate.hidden)
            ?.focus({ preventScroll: true });
        }
      });

      control.records.push({
        compactText: compact(optionSearchText(option)),
        group,
        index,
        item,
        option,
        searchText: optionSearchText(option),
        tokens: tokenize(optionSearchText(option)),
        value: String(option.value || "").toLowerCase(),
      });
      control.items.push(item);
      options.append(item);
    });

    options.append(control.empty);
    syncButton(control);
    filterOptions(control);
  }

  function positionControl(control) {
    if (openControl !== control) return;
    const margin = 10;
    const gap = 6;
    const rect = control.button.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const maxWidth = Math.max(180, viewportWidth - margin * 2);
    const width = Math.min(Math.max(rect.width, 220), maxWidth);
    const left = clamp(rect.left, margin, viewportWidth - width - margin);
    const roomBelow = viewportHeight - rect.bottom - margin;
    const roomAbove = rect.top - margin;
    const opensUp = roomBelow < 220 && roomAbove > roomBelow;
    const available = Math.max(140, (opensUp ? roomAbove : roomBelow) - gap);

    control.menu.style.width = `${width}px`;
    control.menu.style.left = `${left}px`;
    control.menu.style.maxHeight = `${Math.min(340, available)}px`;
    control.menu.classList.toggle("opens-up", opensUp);

    const top = opensUp
      ? clamp(rect.top - control.menu.offsetHeight - gap, margin, viewportHeight)
      : clamp(rect.bottom + gap, margin, viewportHeight - margin);
    control.menu.style.top = `${top}px`;
  }

  function openSelect(control) {
    if (control.select.disabled) return;
    if (openControl && openControl !== control) closeControl(openControl);
    openControl = control;
    control.wrapper.classList.add("is-open");
    control.button.setAttribute("aria-expanded", "true");
    control.search.value = "";
    renderOptions(control);
    control.menu.classList.add("is-open");
    positionControl(control);
    requestAnimationFrame(() => {
      control.search.focus({ preventScroll: true });
      positionControl(control);
    });
  }

  function closeControl(control) {
    control.wrapper.classList.remove("is-open");
    control.button.setAttribute("aria-expanded", "false");
    control.menu.classList.remove("is-open", "opens-up");
    if (openControl === control) openControl = null;
  }

  function createControl(select) {
    if (controls.has(select) || select.closest("[data-no-searchable-select]")) {
      return;
    }

    const id = `searchable-select-${++controlId}`;
    const wrapper = document.createElement("span");
    wrapper.className = "searchable-select";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "searchable-select__button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", `${id}-listbox`);
    if (select.getAttribute("aria-label")) {
      button.setAttribute("aria-label", select.getAttribute("aria-label"));
    }

    const value = document.createElement("span");
    value.className = "searchable-select__value";
    button.append(value);

    const menu = document.createElement("span");
    menu.className = "searchable-select__menu";
    menu.id = id;

    const search = document.createElement("input");
    search.type = "search";
    search.className = "searchable-select__search";
    search.autocomplete = "off";
    search.placeholder = "Search";
    search.setAttribute("aria-label", "Search options");

    const options = document.createElement("span");
    options.className = "searchable-select__options";
    options.id = `${id}-listbox`;
    options.setAttribute("role", "listbox");

    const empty = document.createElement("span");
    empty.className = "searchable-select__empty";
    empty.textContent = "No matches";
    empty.hidden = true;
    options.append(empty);

    menu.append(search, options);
    document.body.append(menu);
    select.after(wrapper);
    wrapper.append(button);
    select.classList.add("searchable-select-native");
    select.dataset.searchableSelectNative = "true";

    const control = {
      button,
      empty,
      items: [],
      lastValue: select.value,
      menu,
      observer: null,
      options,
      records: [],
      search,
      select,
      value,
      wrapper,
    };
    controls.set(select, control);

    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (openControl === control) closeControl(control);
      else openSelect(control);
    });

    button.addEventListener("keydown", (event) => {
      if (["ArrowDown", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        openSelect(control);
      }
    });

    search.addEventListener("input", () => {
      filterOptions(control);
      positionControl(control);
    });

    search.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeControl(control);
        button.focus();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        focusVisibleItem(control, 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        focusVisibleItem(control, -1);
      } else if (event.key === "Enter") {
        const first = control.items.find((item) => !item.hidden);
        if (first) {
          event.preventDefault();
          selectOption(control, Number(first.dataset.index));
        }
      }
    });

    select.addEventListener("change", () => {
      control.lastValue = select.value;
      syncButton(control);
      syncSelectedOption(control);
    });

    select.addEventListener("invalid", () => {
      button.setAttribute("aria-invalid", "true");
      button.focus();
    });

    select.addEventListener("input", () => {
      button.removeAttribute("aria-invalid");
    });

    control.observer = new MutationObserver(() => renderOptions(control));
    control.observer.observe(select, {
      attributes: true,
      attributeFilter: [
        "disabled",
        "hidden",
        "label",
        "required",
        "selected",
        "value",
      ],
      childList: true,
      subtree: true,
    });

    renderOptions(control);
  }

  function enhanceAll(root = document) {
    root.querySelectorAll(selectQuery).forEach((select) => {
      if (select.size > 1) return;
      createControl(select);
    });
  }

  function syncAll() {
    controls.forEach((control) => {
      if (control.lastValue !== control.select.value) {
        control.lastValue = control.select.value;
        syncButton(control);
        syncSelectedOption(control);
      }
    });
  }

  document.addEventListener("click", (event) => {
    if (
      openControl &&
      !openControl.wrapper.contains(event.target) &&
      !openControl.menu.contains(event.target)
    ) {
      closeControl(openControl);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openControl) closeControl(openControl);
  });

  window.addEventListener("resize", () => {
    if (openControl) positionControl(openControl);
  });
  window.addEventListener(
    "scroll",
    () => {
      if (openControl) positionControl(openControl);
    },
    true,
  );

  const bodyObserver = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) enhanceAll(node);
      });
    });
  });

  function init() {
    enhanceAll();
    bodyObserver.observe(document.body, { childList: true, subtree: true });
    window.setInterval(syncAll, 300);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
