/*
 * Behaviour tests for static/js/searchable-select.js (the type-to-search
 * dropdown used on every long <select> in the app).
 *
 *   npm install jsdom --no-save
 *   node tests/js/searchable-select.test.mjs
 */
import { JSDOM } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = fs.readFileSync(path.join(here, '..', '..', 'static', 'js', 'searchable-select.js'), 'utf8');

const options = Array.from({ length: 200 }, (_, i) =>
  `<option value="${i + 1}">Brake Pad ${10 + i}mm (SKU-${1000 + i})</option>`).join('');

const dom = new JSDOM(`<!DOCTYPE html><body>
  <form id="f">
    <select name="product" class="form-select select2" id="prod" required>
      <option value="">---------</option>${options}
    </select>
    <select name="status" class="form-select" id="short">
      <option value="a">Active</option><option value="b">Inactive</option>
    </select>
    <div id="empty-form"><select name="form-__prefix__-product"><option>x</option></select></div>
    <div id="later"></div>
  </form>
</body>`, { runScripts: 'outside-only', pretendToBeVisual: true });

const { window } = dom;
const { document } = window;
window.eval(js);
window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));

const fails = [];
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' :: ' + extra : ''}`);
  if (!cond) fails.push(name);
};

const prod = document.getElementById('prod');
const wrap = prod.closest('.ss-wrap');

check('long select enhanced', !!wrap);
check('short select left native', !document.getElementById('short').closest('.ss-wrap'));
check('formset template select skipped',
      !document.querySelector('#empty-form select').closest('.ss-wrap'));
check('native select kept inside wrap + name intact',
      wrap.querySelector('select') === prod && prod.name === 'product');
check('required attribute preserved', prod.required === true);

const toggle = wrap.querySelector('.ss-toggle');
const panel = wrap.querySelector('.ss-panel');
const search = wrap.querySelector('.ss-search');

check('button is type=button (will not submit form)', toggle.type === 'button');
check('panel hidden initially', panel.hidden === true);
check('placeholder shown', toggle.textContent.trim() === '---------');

toggle.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
check('panel opens on click', panel.hidden === false);
check('all options listed on open',
      wrap.querySelectorAll('.ss-option').length === 201,
      `${wrap.querySelectorAll('.ss-option').length} options`);

// --- search filtering
search.value = 'sku-1042';
search.dispatchEvent(new window.Event('input', { bubbles: true }));
let shown = wrap.querySelectorAll('.ss-option');
check('search narrows to one match', shown.length === 1, shown[0] && shown[0].textContent);
check('match is highlighted', shown[0].innerHTML.includes('<mark>'));

// multi-term, out of order
search.value = '99mm brake';
search.dispatchEvent(new window.Event('input', { bubbles: true }));
shown = wrap.querySelectorAll('.ss-option');
// "99mm" also legitimately matches "199mm", so assert on content not count
check('multi-word search matches out of order',
      shown.length === 2 && shown[0].textContent.includes('99mm') && shown[1].textContent.includes('199mm'),
      Array.from(shown).map(e => e.textContent).join(' | '));

search.value = 'zzzz';
search.dispatchEvent(new window.Event('input', { bubbles: true }));
check('no matches shows empty state',
      wrap.querySelectorAll('.ss-option').length === 0 && !wrap.querySelector('.ss-empty').hidden);

// --- choosing by keyboard
let changeFired = 0;
prod.addEventListener('change', () => changeFired++);
search.value = 'sku-1150';
search.dispatchEvent(new window.Event('input', { bubbles: true }));
search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));

check('enter selects the match', prod.value === '151', `value=${prod.value}`);
check('change event fired once', changeFired === 1, `fired ${changeFired}x`);
check('label updated', toggle.textContent.includes('SKU-1150'), toggle.textContent.trim());
check('panel closed after choose', panel.hidden === true);

// --- external change is reflected
prod.value = '3';
prod.dispatchEvent(new window.Event('change', { bubbles: true }));
check('external change syncs label', toggle.textContent.includes('SKU-1002'), toggle.textContent.trim());

// --- escape closes
toggle.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check('escape closes panel', panel.hidden === true);

// --- dynamically added select (formset row) gets enhanced by the observer
const div = document.createElement('div');
div.innerHTML = '<select name="form-1-product" class="product-select"><option>Dyn A</option><option>Dyn B</option></select>';
document.getElementById('later').appendChild(div);
await new Promise(r => setTimeout(r, 20));
check('dynamically added select enhanced',
      !!document.querySelector('[name="form-1-product"]').closest('.ss-wrap'));

// --- form submission still carries the native value
prod.value = '7';
const data = new window.FormData(document.getElementById('f'));
check('form posts the native select value', data.get('product') === '7', String(data.get('product')));

console.log(fails.length ? `\n${fails.length} FAILURES` : '\nALL PASS');
process.exit(fails.length ? 1 : 0);
