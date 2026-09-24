/*
 * Table search.
 *
 * Every list page gets a search box that filters the rows on screen by matching
 * against ALL columns at once — invoice number, name, phone, amount, date,
 * status, whatever the table happens to show. Type several words and a row has
 * to match all of them.
 *
 * Applied automatically to any table with enough rows. Opt out per table with
 * data-no-search (used on receipts and other detail tables that are not lists).
 *
 * This filters what the page is showing. Pages that also have a server-side
 * search (the "Search" field in a filter form) keep it — that one queries the
 * whole database, this one narrows the results in front of you.
 */
(function () {
    'use strict';

    var MIN_ROWS = 5;

    function isPlaceholderRow(row) {
        // The "No transactions found" style row every list template ends with.
        var cells = row.children;
        return cells.length === 1 && cells[0].hasAttribute('colspan');
    }

    function isDetailRow(row) {
        // An expandable breakdown (e.g. the batches behind a product's total).
        // It belongs to the row above it, not to the list.
        return row.classList.contains('collapse') || row.hasAttribute('data-ts-detail');
    }

    function dataRows(table) {
        var body = table.tBodies[0];
        if (!body) return [];
        return Array.prototype.filter.call(body.rows, function (row) {
            return !isPlaceholderRow(row) && !isDetailRow(row);
        });
    }

    function shouldEnhance(table) {
        if (table.dataset.tsReady) return false;
        if (table.closest('[data-no-search]') || table.hasAttribute('data-no-search')) return false;
        return dataRows(table).length >= MIN_ROWS;
    }

    function normalise(text) {
        return (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
    }

    function enhanceTable(table) {
        table.dataset.tsReady = '1';

        var rows = dataRows(table);
        var placeholder = Array.prototype.find.call(
            (table.tBodies[0] || {}).rows || [], isPlaceholderRow
        );
        var columnCount = (table.tHead && table.tHead.rows[0]) ? table.tHead.rows[0].cells.length : 1;

        // Cache each row's searchable text once. A row's detail rows (its
        // collapsed breakdown) count as part of it, so searching a batch number
        // finds the product it sits under.
        rows.forEach(function (row) {
            var text = row.textContent;
            var next = row.nextElementSibling;
            while (next && isDetailRow(next)) {
                text += ' ' + next.textContent;
                next = next.nextElementSibling;
            }
            row.dataset.tsText = normalise(text);
            row.tsDetails = [];
            next = row.nextElementSibling;
            while (next && isDetailRow(next)) {
                row.tsDetails.push(next);
                next = next.nextElementSibling;
            }
        });

        // The page may already have a server-side search; say what this one does.
        var hasServerSearch = !!document.querySelector('form input[name="q"]');
        var label = hasServerSearch ? 'Filter these results...' : 'Search...';

        var bar = document.createElement('div');
        bar.className = 'table-search-bar d-flex flex-wrap align-items-center gap-2 px-3 py-2';
        bar.innerHTML =
            '<div class="input-group input-group-merge table-search-input">' +
            '<span class="input-group-text"><i class="ri ri-search-line"></i></span>' +
            '<input type="search" class="form-control" placeholder="' + label + '" ' +
            'aria-label="Search this table" autocomplete="off">' +
            '</div>' +
            '<small class="text-muted table-search-count"></small>';

        var container = table.closest('.table-responsive') || table;
        container.parentNode.insertBefore(bar, container);

        var input = bar.querySelector('input');
        var count = bar.querySelector('.table-search-count');

        var noMatch = document.createElement('tr');
        noMatch.className = 'table-search-empty';
        noMatch.hidden = true;
        noMatch.innerHTML = '<td colspan="' + columnCount + '" class="text-center py-4 text-muted">' +
            'Nothing on this page matches your search.</td>';
        if (table.tBodies[0]) table.tBodies[0].appendChild(noMatch);

        function apply() {
            var terms = normalise(input.value).split(' ').filter(Boolean);
            var shown = 0;

            rows.forEach(function (row) {
                var hit = terms.every(function (term) {
                    return row.dataset.tsText.indexOf(term) !== -1;
                });
                row.hidden = !hit;
                // hidden alone loses to `display: table-row` in some themes.
                row.style.display = hit ? '' : 'none';
                // A filtered-out row takes its breakdown with it; a matching one
                // hands its breakdown back to Bootstrap's collapse.
                (row.tsDetails || []).forEach(function (detail) {
                    detail.style.display = hit ? '' : 'none';
                });
                if (hit) shown++;
            });

            var searching = terms.length > 0;
            noMatch.hidden = !(searching && shown === 0);
            noMatch.style.display = noMatch.hidden ? 'none' : '';
            if (placeholder) placeholder.style.display = searching ? 'none' : '';
            count.textContent = searching ? shown + ' of ' + rows.length + ' shown' : '';
        }

        var timer = null;
        input.addEventListener('input', function () {
            window.clearTimeout(timer);
            timer = window.setTimeout(apply, 80);
        });
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                input.value = '';
                apply();
            }
        });

        apply();
    }

    function enhance(root) {
        var scope = root && root.querySelectorAll ? root : document;
        Array.prototype.forEach.call(scope.querySelectorAll('table'), function (table) {
            if (!shouldEnhance(table)) return;
            try {
                enhanceTable(table);
            } catch (err) {
                if (window.console) console.warn('table-search:', err);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        enhance(document);
    });

    window.enhanceTableSearch = enhance;
})();
