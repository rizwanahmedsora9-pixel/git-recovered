/* =========================================================================
   Profit & Loss — automatic filters and searchable dropdowns.

   Responsibilities
   ----------------
   * Every filter change refreshes the report immediately (no Apply button).
     Free-text fields are debounced; selects and dates apply at once.
   * Only the results region is re-rendered (`?partial=1`), so the filters keep
     their focus, scroll position and selected values.
   * A refresh NEVER moves the reader.  The window scroll position, the
     scroll offsets of the inner tables, open <details> sections and focus
     inside the results are captured just before the swap and restored in
     the same frame, so the live auto-refresh (ledger_live.js) can update
     the figures underneath someone who is reading at the bottom of the
     page without pulling them back to the top.  The only exception is an
     explicit pagination click, which moves to the top of its own table.
   * While a filter request is in flight the results are dimmed and a
     progress bar is shown, so stale figures are never presented as
     current.  Background live refreshes skip the dimming entirely.
   * All arithmetic stays on the server.  This script never computes a total.

   Loaded only by the P&L page; no dependency on any other module.
   ========================================================================= */
(function () {
    "use strict";

    var DEBOUNCE_MS = 500;
    var activeRefresh = null;

    function each(root, selector, fn) {
        (root || document).querySelectorAll(selector).forEach(fn);
    }

    function normalise(value) {
        return String(value == null ? "" : value).trim().toLowerCase();
    }

    /* ---------------------------------------------------------------- combobox */
    function Combo(wrapper, config) {
        this.wrapper = wrapper;
        this.config = config || {};
        this.options = [];
        this.activeIndex = -1;
        if (!wrapper) {
            // The page rendered without this control; stay inert instead of
            // throwing and taking the whole filter bar down with us.
            this.input = null;
            this.menu = null;
            this.hidden = null;
            this.categoryHidden = null;
            return;
        }
        this.role = wrapper.getAttribute("data-combo");
        this.input = wrapper.querySelector("[data-combo-input]");
        this.menu = wrapper.querySelector("[data-combo-menu]");
        this.hidden = wrapper.querySelector("[data-combo-value]");
        this.categoryHidden = wrapper.querySelector("[data-combo-category]");
        if (!this.input || !this.menu) return;
        this.bind();
        this.refreshOptions();
    }

    Combo.prototype.entityType = function () {
        var select = document.getElementById("plEntityType");
        return select ? select.value : "";
    };

    Combo.prototype.isCategoryMode = function () {
        return this.role === "client" && this.entityType() === "category";
    };

    Combo.prototype.refreshOptions = function () {
        if (!this.wrapper) return;
        var data = this.config.data || {};
        if (this.role === "material") {
            this.options = (data.materials || []).map(function (name) {
                return { value: name, hint: "" };
            });
        } else if (this.isCategoryMode()) {
            this.options = (data.categories || []).map(function (name) {
                return { value: name, hint: "category" };
            });
        } else {
            var names = data.customers || [];
            var codes = data.customerCodes || [];
            this.options = names.map(function (name, index) {
                return { value: name, hint: codes[index] || "" };
            });
        }
    };

    Combo.prototype.matches = function (query) {
        var needle = normalise(query);
        if (!needle) return this.options.slice(0, 200);
        var out = [];
        for (var i = 0; i < this.options.length && out.length < 200; i += 1) {
            var option = this.options[i];
            if (normalise(option.value).indexOf(needle) >= 0 || normalise(option.hint).indexOf(needle) >= 0) {
                out.push(option);
            }
        }
        return out;
    };

    Combo.prototype.renderMenu = function (query) {
        var menu = this.menu;
        if (!menu || !this.wrapper) return;
        var list = this.matches(query);
        menu.innerHTML = "";
        if (!list.length) {
            var empty = document.createElement("div");
            empty.className = "pl-combo-empty";
            empty.textContent = "No matches";
            menu.appendChild(empty);
            menu.hidden = false;
            this.setExpanded(true);
            return;
        }
        var self = this;
        list.forEach(function (option, index) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "pl-combo-option" + (index === self.activeIndex ? " is-active" : "");
            button.setAttribute("data-value", option.value);
            button.setAttribute("role", "option");
            button.textContent = option.value;
            if (option.hint) {
                var hint = document.createElement("span");
                hint.className = "pl-combo-hint";
                hint.textContent = option.hint;
                button.appendChild(hint);
            }
            button.addEventListener("mousedown", function (ev) {
                ev.preventDefault();
                self.select(option.value);
            });
            menu.appendChild(button);
        });
        menu.hidden = false;
        this.setExpanded(true);
    };

    Combo.prototype.select = function (value) {
        if (!this.wrapper) return;
        this.input.value = value || "";
        if (this.isCategoryMode()) {
            if (this.categoryHidden) this.categoryHidden.value = value || "";
            if (this.hidden) this.hidden.value = "";
        } else {
            if (this.hidden) this.hidden.value = value || "";
            if (this.categoryHidden) this.categoryHidden.value = "";
        }
        this.close();
        this.config.onCommit && this.config.onCommit();
    };

    Combo.prototype.setExpanded = function (open) {
        if (this.input) this.input.setAttribute("aria-expanded", open ? "true" : "false");
    };

    Combo.prototype.close = function () {
        this.activeIndex = -1;
        if (this.menu) this.menu.hidden = true;
        this.setExpanded(false);
    };

    Combo.prototype.bind = function () {
        var self = this;
        this.input.addEventListener("focus", function () {
            self.renderMenu(self.input.value);
        });
        this.input.addEventListener("input", function () {
            self.activeIndex = -1;
            self.renderMenu(self.input.value);
            self.config.onType && self.config.onType(self.input.value);
        });
        this.input.addEventListener("blur", function () {
            // Selecting by click fires mousedown first; delay closing so the
            // click is not lost, then fall back to the typed text.
            window.setTimeout(function () {
                self.close();
                self.config.onBlur && self.config.onBlur(self.input.value);
            }, 140);
        });
        this.input.addEventListener("keydown", function (ev) {
            var list = self.menu ? self.menu.querySelectorAll(".pl-combo-option") : [];
            if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
                if (!list.length) return;
                ev.preventDefault();
                self.activeIndex += ev.key === "ArrowDown" ? 1 : -1;
                if (self.activeIndex < 0) self.activeIndex = list.length - 1;
                if (self.activeIndex >= list.length) self.activeIndex = 0;
                Array.prototype.forEach.call(list, function (el, index) {
                    el.classList.toggle("is-active", index === self.activeIndex);
                });
            } else if (ev.key === "Enter") {
                if (self.activeIndex >= 0 && list[self.activeIndex]) {
                    ev.preventDefault();
                    self.select(list[self.activeIndex].getAttribute("data-value"));
                }
            } else if (ev.key === "Escape") {
                self.close();
            }
        });
        document.addEventListener("click", function (ev) {
            if (!self.wrapper.contains(ev.target)) self.close();
        });
    };

    /* ------------------------------------------------------------ controller */
    function init(config) {
        var form = document.querySelector(config.form || "#plFilterForm");
        var region = document.querySelector(config.region || "#plResultsRegion");
        if (!form || !region) return;

        var loadingBar = document.querySelector(config.loadingBar || "#plLoadingBar");
        var stateLabel = document.querySelector(config.stateLabel || "#plFilterState");
        var data = config.data || {};
        var controller = null;
        var timer = null;
        var lastUrl = window.location.href;

        function setBusy(busy, options) {
            var surface = region.querySelector("[data-pl-results]") || region;
            // A background (live) refresh must not flash/dim the report under
            // a reader: only the thin progress bar and the status label move.
            var background = !!(options && options.background);
            surface.classList.toggle("is-loading", busy && !background);
            if (loadingBar) loadingBar.classList.toggle("is-on", busy);
            if (stateLabel) {
                stateLabel.classList.toggle("is-busy", busy);
                stateLabel.innerHTML = busy
                    ? '<i class="bi bi-arrow-repeat"></i> Updating…'
                    : '<i class="bi bi-cloud-check"></i> Filters apply automatically';
            }
        }

        function partialName() {
            return config.partialParam || "partial";
        }

        function absolute(value) {
            try {
                return new URL(value, window.location.href);
            } catch (error) {
                return null;
            }
        }

        /* Link targets are ordinary bookmarks: they carry no ``partial`` flag.
           The fetch needs it, the address bar must not. */
        function splitPartial(url) {
            var parsed = absolute(url);
            if (!parsed) return { fetchUrl: url, cleanUrl: url };
            var clean = new URL(parsed.toString());
            clean.searchParams.delete(partialName());
            return {
                fetchUrl: parsed.toString() + (parsed.search ? "&" : "?") + partialName() + "=1",
                cleanUrl: clean.pathname + (clean.search || "")
            };
        }

        function buildUrl(overrides) {
            var params = new URLSearchParams();
            each(form, "input, select", function (field) {
                if (!field.name) return;
                if (field.disabled) return;
                var type = (field.type || "").toLowerCase();
                if ((type === "checkbox" || type === "radio") && !field.checked) return;
                var value = String(field.value == null ? "" : field.value).trim();
                if (!value) return;
                params.set(field.name, value);
            });
            if (overrides) {
                Object.keys(overrides).forEach(function (key) {
                    if (overrides[key] === null || overrides[key] === "") params.delete(key);
                    else params.set(key, overrides[key]);
                });
            }
            params.set(partialName(), "1");
            return form.getAttribute("action") + "?" + params.toString();
        }

        /* The server resolves presets ("this month") into concrete dates.  Show
           them in the date fields so a later manual edit continues from the
           range the reader is actually looking at. */
        function syncDateInputs() {
            var surface = region.querySelector("[data-pl-results]");
            if (!surface) return;
            [["start_date", "data-pl-start"], ["end_date", "data-pl-end"]].forEach(function (pair) {
                var field = form.querySelector('[name="' + pair[0] + '"]');
                if (!field) return;
                var value = surface.getAttribute(pair[1]) || "";
                if (field.value !== value) field.value = value;
            });
        }

        function clearError() {
            var existing = region.querySelector(".pl-error");
            if (existing) existing.remove();
        }

        function showError(message) {
            clearError();
            var box = document.createElement("div");
            box.className = "pl-error";
            box.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i> ' + message;
            region.insertBefore(box, region.firstChild);
        }

        /* --- in-place refresh: never move the reader ------------------------
           Replacing the results region must be invisible to someone reading
           it.  Capture the reading position immediately before the swap
           (not when the request starts — the reader may keep scrolling
           while it is in flight) and restore it in the same frame:
           window scroll (adjusted by an anchor element so height changes
           above the reading line do not shift the text), inner table
           scroll offsets, open <details> sections and focus. */
        var SCROLLER_SELECTOR = ".pl-scroll, .pl-chart, .ui-table-wrap";

        function domPath(el) {
            if (!el || !region.contains(el)) return null;
            var path = [];
            var node = el;
            while (node && node !== region) {
                var parent = node.parentElement;
                if (!parent) return null;
                path.unshift(Array.prototype.indexOf.call(parent.children, node));
                node = parent;
            }
            return path;
        }

        function fromDomPath(path) {
            if (!path) return null;
            var node = region;
            for (var i = 0; i < path.length; i += 1) {
                node = node.children ? node.children[path[i]] : null;
                if (!node) return null;
            }
            return node === region ? null : node;
        }

        function findAnchor() {
            var el = null;
            try {
                if (document.elementFromPoint) {
                    var rect = region.getBoundingClientRect();
                    var x = Math.max(1, Math.min((window.innerWidth || 2) - 2, rect.left + 24));
                    el = document.elementFromPoint(x, 40);
                    if (!el || !region.contains(el)) {
                        el = document.elementFromPoint(x, Math.floor((window.innerHeight || 90) / 3));
                    }
                    if (el && !region.contains(el)) el = null;
                }
            } catch (error) {
                el = null;
            }
            if (!el) {
                var blocks = region.querySelectorAll(".ui-card, .profit-hero, .pl-section-head, tr");
                for (var i = 0; i < blocks.length; i += 1) {
                    var blockRect = blocks[i].getBoundingClientRect();
                    if (blockRect.bottom > 0 && blockRect.top < (window.innerHeight || 0)) {
                        el = blocks[i];
                        break;
                    }
                }
            }
            var path = el ? domPath(el) : null;
            if (!path) return null;
            return { path: path, top: el.getBoundingClientRect().top };
        }

        function captureViewState() {
            var scrollers = [];
            var scrollEls = region.querySelectorAll(SCROLLER_SELECTOR);
            Array.prototype.forEach.call(scrollEls, function (el, index) {
                if (el.scrollTop || el.scrollLeft) {
                    scrollers.push({ index: index, top: el.scrollTop, left: el.scrollLeft });
                }
            });
            var details = [];
            Array.prototype.forEach.call(region.querySelectorAll("details"), function (el) {
                details.push(!!el.open);
            });
            var active = document.activeElement;
            var focus = null;
            if (active && active !== document.body && region.contains(active)) {
                focus = {
                    path: domPath(active),
                    start: typeof active.selectionStart === "number" ? active.selectionStart : null,
                    end: typeof active.selectionEnd === "number" ? active.selectionEnd : null
                };
                if (!focus.path) focus = null;
            }
            return {
                x: window.pageXOffset || 0,
                y: window.pageYOffset || 0,
                anchor: findAnchor(),
                scrollers: scrollers,
                details: details,
                focus: focus
            };
        }

        function scrollWindowTo(x, y) {
            if (typeof window.scrollTo === "function") {
                window.scrollTo({ left: x, top: y, behavior: "auto" });
            }
        }

        // ``navElement`` is set only for an explicit pagination click: the
        // reader asked for another page of that table, so land on the top
        // of the table's card — never on the top of the whole report.
        function restoreViewState(state, navElement) {
            if (!state) return;
            Array.prototype.forEach.call(region.querySelectorAll("details"), function (el, index) {
                if (index < state.details.length) el.open = state.details[index];
            });
            var scrollEls = region.querySelectorAll(SCROLLER_SELECTOR);
            state.scrollers.forEach(function (entry) {
                var el = scrollEls[entry.index];
                if (!el) return;
                el.scrollTop = entry.top;
                el.scrollLeft = entry.left;
            });

            var targetY;
            if (navElement) {
                targetY = navElement.getBoundingClientRect().top + (window.pageYOffset || 0) - 12;
                scrollWindowTo(state.x, Math.max(0, targetY));
                return;
            }

            targetY = state.y;
            if (state.anchor) {
                var anchorEl = fromDomPath(state.anchor.path);
                if (anchorEl) {
                    // Keep the anchored element at the same viewport offset
                    // even if content above it grew or shrank in the refresh.
                    targetY = (window.pageYOffset || 0)
                        + (anchorEl.getBoundingClientRect().top - state.anchor.top);
                }
            }
            scrollWindowTo(state.x, Math.max(0, targetY));

            if (state.focus) {
                var focusEl = fromDomPath(state.focus.path);
                if (focusEl && typeof focusEl.focus === "function") {
                    try {
                        focusEl.focus({ preventScroll: true });
                    } catch (error) {
                        focusEl.focus();
                    }
                    if (state.focus.start !== null && typeof focusEl.setSelectionRange === "function") {
                        try { focusEl.setSelectionRange(state.focus.start, state.focus.end); } catch (error) { /* not a text field */ }
                    }
                    // Belt and braces for browsers that scroll on focus anyway.
                    scrollWindowTo(state.x, Math.max(0, targetY));
                }
            }
        }

        function load(url, options) {
            options = options || {};
            if (controller) controller.abort();
            controller = window.AbortController ? new AbortController() : null;
            setBusy(true, options);
            clearError();
            var target = splitPartial(url);
            return fetch(target.fetchUrl, {
                headers: { "X-Requested-With": "fetch" },
                credentials: "same-origin",
                signal: controller ? controller.signal : undefined
            }).then(function (response) {
                if (!response.ok) throw new Error("The report could not be generated (HTTP " + response.status + ").");
                return response.text();
            }).then(function (html) {
                // In-place swap only: capture the reading position right
                // now, replace the figures, put the reader back exactly
                // where they were.  No full reload, no jump to the top.
                var view = captureViewState();
                region.innerHTML = html;
                syncDateInputs();
                var cleanUrl = target.cleanUrl;
                var navElement = options.scrollToPath ? fromDomPath(options.scrollToPath) : null;
                restoreViewState(view, navElement);
                lastUrl = cleanUrl;
                window.history.replaceState({}, "", cleanUrl);
                setBusy(false);
                region.dispatchEvent(new CustomEvent("pl:updated"));
                return true;
            }).catch(function (error) {
                if (error && error.name === "AbortError") return true;
                setBusy(false);
                showError((error && error.message ? error.message : "The report could not be updated.") + " <a href=\"" + splitPartial(url).cleanUrl + "\">Open the full page</a>.");
                return false;
            });
        }

        function refresh(overrides, options) {
            window.clearTimeout(timer);
            return load(buildUrl(overrides), options);
        }
        // The live-ledger watcher calls this after another user posts a sale or
        // GRN.  It uses exactly the same partial request as a filter change
        // (with {background: true}); both replace only the results region
        // and restore the reading position, so neither pulls the reader up.
        activeRefresh = refresh;

        function refreshDebounced(overrides) {
            window.clearTimeout(timer);
            timer = window.setTimeout(function () { refresh(overrides); }, DEBOUNCE_MS);
        }

        /* --- filters ------------------------------------------------------ */
        /* A preset period owns the dates; editing a date therefore means an
           explicit custom range, otherwise the typed dates would be ignored. */
        function markCustomPeriod() {
            var periodSelect = form.querySelector('[name="period"]');
            if (!periodSelect) return;
            if (periodSelect.value === "custom") return;
            var option = periodSelect.querySelector('option[value="custom"]');
            if (!option) return;
            periodSelect.value = "custom";
        }

        each(form, "[data-pl-auto]", function (field) {
            field.addEventListener("change", function () {
                if (field.name === "entity_type") comboClient.refreshOptions();
                if (field.name === "start_date" || field.name === "end_date") {
                    markCustomPeriod();
                } else if (field.name === "period" && field.value !== "custom") {
                    // Presets recompute both dates on the server; clear the
                    // stale values so they are not submitted with the preset.
                    var start = form.querySelector('[name="start_date"]');
                    var end = form.querySelector('[name="end_date"]');
                    if (start) start.value = "";
                    if (end) end.value = "";
                }
                refresh();
            });
        });
        each(form, "[data-pl-debounce]", function (field) {
            field.addEventListener("input", function () { refreshDebounced(); });
            field.addEventListener("change", function () { refresh(); });
        });

        var comboClient = new Combo(form.querySelector('[data-combo="client"]'), {
            data: data,
            onCommit: function () { refresh(); },
            onType: function (value) {
                // In customer mode the typed text is itself a valid filter
                // (matched by name or code), so apply it after a pause.
                if (comboClient.isCategoryMode()) return;
                comboClient.hidden.value = value || "";
                comboClient.categoryHidden.value = "";
                refreshDebounced();
            },
            onBlur: function (value) {
                if (comboClient.isCategoryMode()) {
                    // Category filtering is exact: only commit a real category.
                    var known = (data.categories || []).some(function (name) {
                        return normalise(name) === normalise(value);
                    });
                    if (!known) {
                        comboClient.input.value = comboClient.categoryHidden.value || "";
                        return;
                    }
                    comboClient.select(value);
                    return;
                }
                comboClient.hidden.value = value || "";
                refresh();
            }
        });

        var comboMaterial = new Combo(form.querySelector('[data-combo="material"]'), {
            data: data,
            onCommit: function () { refresh(); },
            onType: function (value) {
                comboMaterial.hidden.value = value || "";
                refreshDebounced();
            },
            onBlur: function (value) {
                comboMaterial.hidden.value = value || "";
                refresh();
            }
        });

        var entitySelect = document.getElementById("plEntityType");
        if (entitySelect) {
            entitySelect.addEventListener("change", function () {
                var label = form.querySelector("[data-pl-entity-label]");
                if (label) label.textContent = entitySelect.value === "category" ? "Client category" : "Customer / company site";
                if (comboClient) {
                    comboClient.hidden.value = "";
                    comboClient.categoryHidden.value = "";
                    comboClient.input.value = "";
                    comboClient.input.placeholder = entitySelect.value === "category"
                        ? "All categories" : "All customers — type to search";
                    comboClient.refreshOptions();
                }
            });
        }

        form.addEventListener("submit", function (ev) {
            ev.preventDefault();
            refresh();
        });

        /* --- navigation inside the results region ------------------------- */
        region.addEventListener("click", function (ev) {
            var link = ev.target.closest("a[data-pl-nav]");
            if (!link) return;
            var href = link.getAttribute("href");
            if (!href || href === "#") {
                ev.preventDefault();
                return;
            }
            ev.preventDefault();
            var options = {};
            // Sorting keeps the reader exactly where they are (the default).
            // Paging is an explicit request for the next slice of a table, so
            // land on that table's card — never on the top of the report.
            if (link.closest(".pagination")) {
                var card = link.closest(".ui-card");
                var cardPath = card ? domPath(card) : null;
                if (cardPath) options.scrollToPath = cardPath;
            }
            load(href, options);
        });

        window.addEventListener("popstate", function () {
            // Never a full reload (which throws the reader back to the top):
            // refresh the results region in place and keep the scroll put.
            // Hash-only changes (e.g. #pl-undelivered-estimate) need no fetch.
            var last = absolute(lastUrl);
            var lastKey = last ? last.pathname + last.search : "";
            if ((window.location.pathname + window.location.search) === lastKey) return;
            load(window.location.href, { keepScroll: true, background: true });
        });

        setBusy(false);
        if (lastUrl) window.history.replaceState({}, "", lastUrl);
    }

    window.FBMProfitFilters = {
        init: init,
        // ``options`` is forwarded to load(): the live watcher passes
        // {keepScroll: true, background: true} for its automatic refresh.
        // Keeping scroll is the default for every refresh regardless.
        refresh: function (overrides, options) {
            return activeRefresh ? activeRefresh(overrides, options) : Promise.resolve(true);
        }
    };
})();
