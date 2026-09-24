/*
 * Behaviour tests for static/js/table-search.js (the all-column search box
 * added to every list table).
 *
 *   npm install jsdom --no-save
 *   node tests/js/table-search.test.mjs
 */
import { JSDOM } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = fs.readFileSync(path.join(here, '..', '..', 'static', 'js', 'table-search.js'), 'utf8');

const SALES = [
  ['#INV-1001', '24 Sep 2026', 'Ama Mensah', '0244000111', 'In-Store', '120.00', 'Completed'],
  ['#INV-1002', '24 Sep 2026', 'Kofi Boateng', '0201234567', 'Delivery', '450.50', 'Completed'],
  ['#INV-1003', '24 Sep 2026', 'Guest', '', 'In-Store', '75.00', 'Refunded'],
  ['#INV-1004', '23 Sep 2026', 'Ama Mensah', '0244000111', 'Delivery', '999.99', 'Pending'],
  ['#INV-1005', '23 Sep 2026', 'Yaw Owusu', '0555000222', 'In-Store', '60.00', 'Completed'],
  ['#INV-1006', '22 Sep 2026', 'Akosua Addo', '0277000333', 'In-Store', '15.25', 'Completed'],
];

function buildDom(rows = SALES, extra = '') {
  const body = rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('');
  return new JSDOM(`<!DOCTYPE html><body>
    <div class="card">
      <div class="table-responsive">
        <table class="table" id="sales">
          <thead><tr><th>Invoice</th><th>Date</th><th>Customer</th><th>Phone</th><th>Type</th><th>Amount</th><th>Status</th></tr></thead>
          <tbody>${body}
            <tr><td colspan="7">No transactions found.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
    ${extra}
  </body>`, { runScripts: 'outside-only', pretendToBeVisual: true });
}

function boot(dom) {
  dom.window.eval(js);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded', { bubbles: true }));
  return dom.window.document;
}

const fails = [];
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' :: ' + extra : ''}`);
  if (!cond) fails.push(name);
};

const dom = buildDom();
const document = boot(dom);
const table = document.getElementById('sales');
const bar = document.querySelector('.table-search-bar');
const input = bar && bar.querySelector('input');
const count = bar && bar.querySelector('.table-search-count');

const visibleRows = () => Array.from(table.tBodies[0].rows)
  .filter(r => r.style.display !== 'none' && !r.classList.contains('table-search-empty'))
  .filter(r => !(r.children.length === 1 && r.children[0].hasAttribute('colspan')));

function search(term) {
  input.value = term;
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  // the handler is debounced
  return new Promise(r => setTimeout(r, 120));
}

check('search box injected above the table', !!bar && !!input);
check('box sits outside the scroll container',
      bar.nextElementSibling && bar.nextElementSibling.classList.contains('table-responsive'));
check('all rows visible before searching', visibleRows().length === 6);
check('placeholder row untouched before searching',
      table.querySelector('td[colspan]').parentElement.style.display !== 'none');

// --- every column is searchable
await search('Kofi');
check('matches the customer column', visibleRows().length === 1, visibleRows()[0].textContent.trim().slice(0, 8));

await search('0555000222');
check('matches the phone column', visibleRows().length === 1);

await search('999.99');
check('matches the amount column', visibleRows().length === 1);

await search('Refunded');
check('matches the status column', visibleRows().length === 1);

await search('INV-1006');
check('matches the invoice column', visibleRows().length === 1);

await search('23 Sep');
// 3, not 2: searching ALL columns means "23" also matches inside the phone
// number 0201234567 on the 24 Sep row. That is the point of the feature.
check('matches the date column', visibleRows().length === 3,
      visibleRows().map(r => r.cells[0].textContent).join());
await search('23 Sep 2026 Yaw');
check('extra terms narrow a date match to one row', visibleRows().length === 1);

await search('delivery');
check('matches the type column and is case-insensitive', visibleRows().length === 2);

// --- multi-term
await search('ama delivery');
check('all terms must match (AND, any column, any order)', visibleRows().length === 1,
      visibleRows().map(r => r.cells[0].textContent).join());

await search('ama in-store');
check('multi-term across columns narrows correctly', visibleRows().length === 1);

// --- empty state
await search('zzzzz');
check('no matches hides every row', visibleRows().length === 0);
const emptyRow = table.querySelector('.table-search-empty');
check('no-match message shown', emptyRow && emptyRow.style.display !== 'none');
check('original placeholder hidden while searching',
      table.querySelector('td[colspan="7"]').parentElement.style.display === 'none');

// --- counter
await search('ama');
check('counter reports matches', /2 of 6 shown/.test(count.textContent), count.textContent);

// --- clearing restores
await search('');
check('clearing restores every row', visibleRows().length === 6);
check('counter cleared', count.textContent === '');
check('no-match row hidden again', table.querySelector('.table-search-empty').style.display === 'none');

// --- escape clears
await search('kofi');
input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check('escape clears the filter', input.value === '' && visibleRows().length === 6);

// --- opt out and threshold
{
  const d2 = new JSDOM(`<!DOCTYPE html><body>
    <table data-no-search id="receipt"><thead><tr><th>Item</th><th>Qty</th></tr></thead><tbody>
      ${Array.from({ length: 9 }, (_, i) => `<tr><td>Item ${i}</td><td>1</td></tr>`).join('')}
    </tbody></table>
    <table id="short"><thead><tr><th>A</th></tr></thead><tbody>
      <tr><td>one</td></tr><tr><td>two</td></tr>
    </tbody></table>
  </body>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  const doc2 = boot(d2);
  check('data-no-search table gets no search box', doc2.querySelectorAll('.table-search-bar').length === 0);
  check('short table gets no search box', !doc2.getElementById('short').dataset.tsReady);
}

// --- expandable detail rows belong to the row above them
{
  const d4 = new JSDOM(`<!DOCTYPE html><body>
    <table id="stock"><thead><tr><th>Product</th><th>Qty</th></tr></thead><tbody>
      <tr><td>Fan Belt</td><td>25</td></tr>
      <tr class="collapse"><td colspan="2"><table data-no-search><tbody>
        <tr><td>LOT-XYZ</td><td>20</td></tr><tr><td>LOT-OLD</td><td>5</td></tr>
      </tbody></table></td></tr>
      <tr><td>Oil Filter</td><td>3</td></tr>
      <tr class="collapse"><td colspan="2">LOT-QQQ</td></tr>
      <tr><td>Spark Plug</td><td>12</td></tr>
      <tr><td>Wiper</td><td>7</td></tr>
      <tr><td>Radiator</td><td>2</td></tr>
      <tr><td>Alternator</td><td>9</td></tr>
    </tbody></table>
  </body>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  const doc4 = boot(d4);
  const t4 = doc4.getElementById('stock');
  const in4 = doc4.querySelector('.table-search-bar input');
  const productRows = () => Array.from(t4.tBodies[0].rows)
    .filter(r => !r.classList.contains('collapse') && !r.classList.contains('table-search-empty'))
    .filter(r => r.style.display !== 'none');

  check('detail rows are not counted as list rows',
        /of 6 shown|^$/.test(in4.parentElement.parentElement.querySelector('.table-search-count').textContent || ''));

  in4.value = 'LOT-XYZ';
  in4.dispatchEvent(new d4.window.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  check('searching a batch number matches its parent product row',
        productRows().length === 1 && productRows()[0].textContent.includes('Fan Belt'),
        productRows().map(r => r.cells[0].textContent).join());

  in4.value = 'oil';
  in4.dispatchEvent(new d4.window.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 120));
  const hiddenDetail = Array.from(t4.tBodies[0].rows)
    .filter(r => r.classList.contains('collapse'))
    .filter(r => r.style.display === 'none');
  check('a filtered-out row hides its breakdown too', hiddenDetail.length === 1);
}

// --- label changes when the page already has a server-side search
{
  const d3 = buildDom(SALES, '<form><input name="q"></form>');
  const doc3 = boot(d3);
  const ph = doc3.querySelector('.table-search-bar input').placeholder;
  check('labelled as a result filter when a server search exists', ph === 'Filter these results...', ph);
}

console.log(fails.length ? `\n${fails.length} FAILURES` : '\nALL PASS');
process.exit(fails.length ? 1 : 0);
