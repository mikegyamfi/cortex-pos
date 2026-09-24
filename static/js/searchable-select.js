/*
 * Searchable select.
 *
 * Any <select> with enough options (products, batches, suppliers, customers...)
 * is upgraded into a type-to-search dropdown, so nobody has to scroll a list of
 * hundreds of products. The original <select> is kept in the DOM and stays the
 * source of truth, so forms post and validate exactly as before.
 *
 * Opt in explicitly with  class="select2" / data-searchable
 * Opt out with            data-no-search  (or <select multiple>)
 *
 * New selects added to the page later (formset rows, modal content) are picked
 * up automatically by a MutationObserver.
 */
(function () {
    'use strict';

    var MIN_OPTIONS = 8;          // shorter lists are fine as native dropdowns
    var FORCE_SELECTOR = '.select2, .product-select, [data-searchable]';
    var SKIP_SELECTOR = '[data-no-search], [multiple], [size]:not([size="1"]), .ss-native';

    function shouldEnhance(select) {
        if (!(select instanceof HTMLSelectElement)) return false;
        if (select.dataset.ssReady) return false;
        if (select.matches(SKIP_SELECTOR)) return false;
        // Formset template rows are cloned as HTML — leave them untouched.
        if (select.name && select.name.indexOf('__prefix__') !== -1) return false;
        if (select.closest('#empty-form, [data-ss-ignore]')) return false;
        if (select.matches(FORCE_SELECTOR)) return true;
        return select.options.length >= MIN_OPTIONS;
    }

    function normalise(text) {
        return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /* Bold the matched terms so it is obvious why a row matched. */
    function highlight(text, terms) {
        var html = escapeHtml(text);
        terms.forEach(function (term) {
            if (!term) return;
            var pattern = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            html = html.replace(new RegExp('(' + pattern + ')(?![^<]*>)', 'gi'), '<mark>$1</mark>');
        });
        return html;
    }

    function SearchableSelect(select) {
        this.select = select;
        this.select.dataset.ssReady = '1';
        this.open = false;
        this.activeIndex = -1;
        this.build();
        this.bind();
        this.syncFromSelect();
    }

    SearchableSelect.prototype.build = function () {
        var select = this.select;

        this.wrap = document.createElement('div');
        this.wrap.className = 'ss-wrap';
        select.parentNode.insertBefore(this.wrap, select);
        this.wrap.appendChild(select);
        select.classList.add('ss-native');
        select.setAttribute('tabindex', '-1');

        this.toggle = document.createElement('button');
        this.toggle.type = 'button';
        this.toggle.className = 'form-select ss-toggle';
        if (select.classList.contains('form-select-sm') || select.classList.contains('form-control-sm')) {
            this.toggle.classList.add('form-select-sm');
        }
        this.toggle.disabled = select.disabled;
        this.toggle.setAttribute('aria-haspopup', 'listbox');
        this.toggle.setAttribute('aria-expanded', 'false');
        this.label = document.createElement('span');
        this.label.className = 'ss-label';
        this.toggle.appendChild(this.label);
        this.wrap.appendChild(this.toggle);

        this.panel = document.createElement('div');
        this.panel.className = 'ss-panel';
        this.panel.hidden = true;
        this.panel.innerHTML =
            '<div class="ss-search-wrap">' +
            '<input type="text" class="form-control form-control-sm ss-search" autocomplete="off" ' +
            'placeholder="Type to search..." aria-label="Search options">' +
            '</div>' +
            '<ul class="ss-options" role="listbox"></ul>' +
            '<div class="ss-empty" hidden>No matches</div>';
        this.wrap.appendChild(this.panel);

        this.search = this.panel.querySelector('.ss-search');
        this.list = this.panel.querySelector('.ss-options');
        this.empty = this.panel.querySelector('.ss-empty');
    };

    /* Read the <option> list off the native select (re-read on every open so
       server-side or JS-driven option changes are always reflected). */
    SearchableSelect.prototype.readOptions = function () {
        this.items = Array.prototype.map.call(this.select.options, function (option, index) {
            var group = option.parentNode && option.parentNode.tagName === 'OPTGROUP'
                ? option.parentNode.label : '';
            return {
                index: index,
                text: option.text,
                group: group,
                disabled: option.disabled,
                haystack: normalise(group + ' ' + option.text)
            };
        });
    };

    SearchableSelect.prototype.render = function (query) {
        var terms = normalise(query).split(' ').filter(Boolean);
        var selectedIndex = this.select.selectedIndex;
        var matches = this.items.filter(function (item) {
            return terms.every(function (term) {
                return item.haystack.indexOf(term) !== -1;
            });
        });

        var html = '';
        var lastGroup = null;
        matches.forEach(function (item) {
            if (item.group && item.group !== lastGroup) {
                html += '<li class="ss-group-label">' + escapeHtml(item.group) + '</li>';
                lastGroup = item.group;
            }
            var classes = 'ss-option' + (item.index === selectedIndex ? ' ss-chosen' : '');
            html += '<li class="' + classes + '" role="option" data-index="' + item.index + '"' +
                (item.disabled ? ' data-disabled="1"' : '') + '>' +
                highlight(item.text, terms) + '</li>';
        });

        this.list.innerHTML = html;
        this.empty.hidden = matches.length > 0;
        this.matchCount = matches.length;

        // Pre-highlight the current selection, or the first result when searching.
        var chosen = this.list.querySelector('.ss-chosen');
        this.setActive(terms.length || !chosen ? 0 : this.optionEls().indexOf(chosen));
    };

    SearchableSelect.prototype.optionEls = function () {
        return Array.prototype.slice.call(this.list.querySelectorAll('.ss-option'));
    };

    SearchableSelect.prototype.setActive = function (index) {
        var els = this.optionEls();
        if (!els.length) {
            this.activeIndex = -1;
            return;
        }
        this.activeIndex = Math.max(0, Math.min(index, els.length - 1));
        els.forEach(function (el, i) {
            el.classList.toggle('ss-active', i === this.activeIndex);
        }, this);
        var active = els[this.activeIndex];
        if (active) {
            var top = active.offsetTop;
            var bottom = top + active.offsetHeight;
            if (top < this.list.scrollTop) this.list.scrollTop = top;
            else if (bottom > this.list.scrollTop + this.list.clientHeight) {
                this.list.scrollTop = bottom - this.list.clientHeight;
            }
        }
    };

    SearchableSelect.prototype.syncFromSelect = function () {
        var option = this.select.options[this.select.selectedIndex];
        var text = option ? option.text.trim() : '';
        this.label.textContent = text || '---------';
        this.label.classList.toggle('ss-placeholder', !text || !this.select.value);
        this.toggle.disabled = this.select.disabled;
    };

    SearchableSelect.prototype.openPanel = function () {
        if (this.open || this.toggle.disabled) return;
        closeAll(this);
        this.readOptions();
        this.search.value = '';
        this.render('');
        this.panel.hidden = false;
        this.open = true;
        this.toggle.setAttribute('aria-expanded', 'true');

        // Flip upwards when there is not enough room below.
        var room = window.innerHeight - this.toggle.getBoundingClientRect().bottom;
        this.panel.classList.toggle('ss-drop-up', room < this.panel.offsetHeight + 16);

        this.search.focus();
    };

    SearchableSelect.prototype.closePanel = function (refocus) {
        if (!this.open) return;
        this.panel.hidden = true;
        this.panel.classList.remove('ss-drop-up');
        this.open = false;
        this.toggle.setAttribute('aria-expanded', 'false');
        if (refocus) this.toggle.focus();
    };

    SearchableSelect.prototype.choose = function (optionIndex) {
        var option = this.select.options[optionIndex];
        if (!option || option.disabled) return;
        this.select.selectedIndex = optionIndex;
        this.syncFromSelect();
        // Let any existing page code (filters that auto-submit, price lookups...) react.
        this.select.dispatchEvent(new Event('input', { bubbles: true }));
        this.select.dispatchEvent(new Event('change', { bubbles: true }));
        this.closePanel(true);
    };

    SearchableSelect.prototype.bind = function () {
        var self = this;

        this.toggle.addEventListener('click', function () {
            self.open ? self.closePanel(true) : self.openPanel();
        });

        this.toggle.addEventListener('keydown', function (event) {
            if (['ArrowDown', 'ArrowUp', 'Enter', ' '].indexOf(event.key) !== -1) {
                event.preventDefault();
                self.openPanel();
            }
        });

        this.search.addEventListener('input', function () {
            self.render(self.search.value);
        });

        this.search.addEventListener('keydown', function (event) {
            switch (event.key) {
                case 'ArrowDown':
                    event.preventDefault();
                    self.setActive(self.activeIndex + 1);
                    break;
                case 'ArrowUp':
                    event.preventDefault();
                    self.setActive(self.activeIndex - 1);
                    break;
                case 'Enter':
                    event.preventDefault();
                    var active = self.optionEls()[self.activeIndex];
                    if (active) self.choose(parseInt(active.dataset.index, 10));
                    break;
                case 'Escape':
                    event.preventDefault();
                    self.closePanel(true);
                    break;
                case 'Tab':
                    self.closePanel(false);
                    break;
            }
        });

        this.list.addEventListener('mousedown', function (event) {
            var option = event.target.closest('.ss-option');
            if (!option) return;
            event.preventDefault();
            self.choose(parseInt(option.dataset.index, 10));
        });

        this.list.addEventListener('mousemove', function (event) {
            var option = event.target.closest('.ss-option');
            if (option) self.setActive(self.optionEls().indexOf(option));
        });

        // Keep the button in step when other code changes the select.
        this.select.addEventListener('change', function () {
            self.syncFromSelect();
        });

        // Browser validation ("please select an item") targets the hidden select.
        this.select.addEventListener('invalid', function () {
            self.toggle.classList.add('is-invalid');
            self.openPanel();
        });
        this.select.addEventListener('change', function () {
            self.toggle.classList.remove('is-invalid');
        });
    };

    var instances = [];

    function closeAll(except) {
        instances.forEach(function (instance) {
            if (instance !== except) instance.closePanel(false);
        });
    }

    function enhance(root) {
        var scope = root && root.querySelectorAll ? root : document;
        var selects = Array.prototype.slice.call(scope.querySelectorAll('select'));
        if (scope !== document && scope.tagName === 'SELECT') selects.push(scope);
        selects.forEach(function (select) {
            if (!shouldEnhance(select)) return;
            try {
                instances.push(new SearchableSelect(select));
            } catch (err) {
                // A broken dropdown must never take the page down with it.
                if (window.console) console.warn('searchable-select:', err);
            }
        });
    }

    document.addEventListener('click', function (event) {
        if (!event.target.closest('.ss-wrap')) closeAll(null);
    });

    document.addEventListener('DOMContentLoaded', function () {
        enhance(document);

        // Formset rows, modal bodies and anything else injected later.
        if (window.MutationObserver) {
            new MutationObserver(function (mutations) {
                mutations.forEach(function (mutation) {
                    Array.prototype.forEach.call(mutation.addedNodes, function (node) {
                        if (node.nodeType === 1) enhance(node);
                    });
                });
            }).observe(document.body, { childList: true, subtree: true });
        }
    });

    // Manual hook for code that builds selects itself.
    window.enhanceSearchableSelects = enhance;
})();
