/*
 * Behaviour tests for static/js/live-search.js (debounced search-as-you-type on
 * list pages' server-side "q" search).
 *
 *   npm install jsdom --no-save
 *   node tests/js/live-search.test.mjs
 */
import { JSDOM } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = fs.readFileSync(path.join(here, '..', '..', 'static', 'js', 'live-search.js'), 'utf8');

const page = (rows, q = '') => `<!DOCTYPE html><body>
  <div class="container-xxl flex-grow-1 container-p-y">
    <form method="get">
      <input type="text" name="q" value="${q}">
      <select name="status"><option value="">All</option><option value="PAID">Paid</option></select>
      <button type="submit">Search</button>
    </form>
    <table id="results"><tbody>${rows.map(r => `<tr><td>${r}</td></tr>`).join('')}</tbody></table>
    <div class="modal" id="m1"><span class="marker">fresh</span></div>
  </div></body>`;

const CATALOGUE = ['Filter - Oil 5W30', 'Air Filter K2', 'Brake Pad Set'];

const dom = new JSDOM(page(CATALOGUE), {
  url: 'http://shop.test/products/?page=3', runScripts: 'outside-only', pretendToBeVisual: true,
});
const { window } = dom;
const doc = window.document;

const requests = [];
window.fetch = (url) => {
  requests.push(url);
  const q = new URL(url).searchParams.get('q') || '';
  const rows = CATALOGUE.filter(name => q.split(/\s+/).every(w => name.toLowerCase().includes(w.toLowerCase())));
  return Promise.resolve({ ok: true, text: () => Promise.resolve(page(rows, q)) });
};

window.eval(js);
doc.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
doc.getElementById('m1').querySelector('.marker').textContent = 'live';

const fails = [];
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' :: ' + extra : ''}`);
  if (!cond) fails.push(name);
};
const wait = ms => new Promise(r => setTimeout(r, ms));
const rows = () => [...doc.querySelectorAll('#results tr')].map(tr => tr.textContent);

const form = doc.querySelector('form');
const input = form.querySelector('input[name="q"]');
input.focus();

// --- typing is debounced: several keystrokes, one request
for (const value of ['o', 'oi', 'oil', 'oil f', 'oil filter']) {
  input.value = value;
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  await wait(30);
}
check('no request while still typing', requests.length === 0);
await wait(450);
check('one request after the pause', requests.length === 1, requests.join(', '));
check('request carries the query', requests[0] && new URL(requests[0]).searchParams.get('q') === 'oil filter');
check('request drops the old page number', requests[0] && !new URL(requests[0]).searchParams.has('page'));

// --- results swapped in place, form kept
check('results replaced', JSON.stringify(rows()) === JSON.stringify(['Filter - Oil 5W30']), rows().join(' | '));
check('the same form element is kept', doc.querySelector('form') === form);
check('search box keeps focus', doc.activeElement === input);
check('only one form on the page', doc.querySelectorAll('form').length === 1);
check('URL updated for back/refresh', window.location.search === '?q=oil+filter', window.location.search);
check('live modal kept rather than a fresh copy',
      doc.getElementById('m1').querySelector('.marker').textContent === 'live');

// --- changing another filter applies at once
const select = form.querySelector('select');
select.value = 'PAID';
select.dispatchEvent(new window.Event('change', { bubbles: true }));
await wait(30);
check('filter change searches immediately', requests.length === 2);
check('filter value sent', requests[1] && new URL(requests[1]).searchParams.get('status') === 'PAID');

// --- submit is handled live, no page navigation
form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await wait(30);
check('submit re-runs the search', requests.length === 3);

// --- opt-out
const optOut = new JSDOM(page(CATALOGUE).replace('<form method="get">', '<form method="get" data-no-search>'),
  { url: 'http://shop.test/products/', runScripts: 'outside-only' });
let optOutRequests = 0;
optOut.window.fetch = () => { optOutRequests++; return new Promise(() => {}); };
optOut.window.eval(js);
optOut.window.document.dispatchEvent(new optOut.window.Event('DOMContentLoaded'));
const optInput = optOut.window.document.querySelector('input[name="q"]');
optInput.value = 'oil';
optInput.dispatchEvent(new optOut.window.Event('input', { bubbles: true }));
await wait(450);
check('data-no-search forms are left alone', optOutRequests === 0);

if (fails.length) {
  console.log(`\n${fails.length} failed`);
  process.exit(1);
}
console.log('\nall passed');
