/*
 * Live search.
 *
 * List pages search the database through a GET form with a "q" box. Without
 * this, you type, then press Search, then wait for a full page load. Here the
 * results refresh by themselves a moment after you stop typing (debounced, so a
 * request goes out per pause, not per keystroke), and the page content is
 * swapped in place — the search box keeps its focus and whatever you have typed
 * since. Changing one of the form's other filters (category, status, dates...)
 * applies it straight away too.
 *
 * Applied to every GET form with an input named "q". Opt out with
 * data-no-search on the form. The Search button still works as before.
 */
(function () {
    'use strict';

    var DELAY = 350;
    var CONTAINER = '.container-xxl.container-p-y';

    function queryUrl(form) {
        var url = new URL(form.getAttribute('action') || window.location.pathname, window.location.href);
        var params = new URLSearchParams();
        new FormData(form).forEach(function (value, key) {
            if (typeof value === 'string' && value.trim() !== '') params.append(key, value);
        });
        // A new search starts from the first page of results.
        params.delete('page');
        url.search = params.toString();
        return url;
    }

    function liveForms(root) {
        return Array.prototype.filter.call(
            (root || document).querySelectorAll('form'),
            function (form) {
                return (form.getAttribute('method') || 'get').toLowerCase() === 'get'
                    && form.querySelector('input[name="q"]')
                    && !form.hasAttribute('data-no-search');
            }
        );
    }

    function wire(form, index) {
        if (form.dataset.liveReady) return;
        form.dataset.liveReady = '1';
        var input = form.querySelector('input[name="q"]');
        var timer = null;
        var lastUrl = queryUrl(form).toString();
        var requestId = 0;

        function run() {
            var url = queryUrl(form);
            if (url.toString() === lastUrl) return;
            lastUrl = url.toString();
            var mine = ++requestId;
            form.classList.add('live-search-busy');

            fetch(url.toString(), {
                credentials: 'same-origin',
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            }).then(function (res) {
                if (!res.ok) throw new Error(res.status);
                return res.text();
            }).then(function (html) {
                // A newer search went out while this one was in flight.
                if (mine !== requestId) return;
                swap(form, index, html);
                window.history.replaceState(null, '', url.toString());
            }).catch(function () {
                // Fall back to an ordinary page load.
                if (mine === requestId) window.location.href = url.toString();
            }).then(function () {
                if (mine === requestId) form.classList.remove('live-search-busy');
            });
        }

        function schedule(delay) {
            window.clearTimeout(timer);
            timer = window.setTimeout(run, delay);
        }

        input.addEventListener('input', function () { schedule(DELAY); });
        form.addEventListener('change', function (event) {
            if (event.target !== input) schedule(0);
        });
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            lastUrl = null;
            schedule(0);
        });
    }

    function swap(form, index, html) {
        var current = document.querySelector(CONTAINER);
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var incoming = doc.querySelector(CONTAINER);
        var newForm = incoming && liveForms(incoming)[index];
        if (!current || !incoming || !newForm) throw new Error('layout changed');

        // Keep the live form (and so the search box, its focus, caret and
        // anything typed since the request went out); take everything else new.
        var active = document.activeElement;
        var caret = null;
        if (active && form.contains(active) && typeof active.selectionStart === 'number') {
            caret = [active.selectionStart, active.selectionEnd];
        }
        newForm.parentNode.replaceChild(form, newForm);
        // Modals are page furniture that page scripts hold references to; keep
        // the live ones rather than the fresh copies.
        Array.prototype.forEach.call(incoming.querySelectorAll('.modal[id]'), function (fresh) {
            var live = document.getElementById(fresh.id);
            if (live && current.contains(live)) fresh.parentNode.replaceChild(live, fresh);
        });
        current.replaceChildren.apply(current, Array.prototype.slice.call(incoming.childNodes));

        if (active && form.contains(active)) {
            active.focus();
            if (caret) {
                try { active.setSelectionRange(caret[0], caret[1]); } catch (e) { /* not a text box */ }
            }
        }
        if (window.enhanceTableSearch) window.enhanceTableSearch(current);
        document.dispatchEvent(new CustomEvent('live-search:updated', { detail: { container: current } }));
    }

    document.addEventListener('DOMContentLoaded', function () {
        liveForms(document.querySelector(CONTAINER) || document).forEach(wire);
    });
})();
