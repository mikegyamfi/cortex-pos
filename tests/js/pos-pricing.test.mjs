/*
 * Behaviour tests for the POS cart's price-tier logic (in sales/pos.html).
 *
 * The functions under test are extracted from the template at run time so the
 * tests always exercise the shipped code, not a copy of it.
 *
 *   npm install jsdom --no-save
 *   node tests/js/pos-pricing.test.mjs
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const template = fs.readFileSync(
  path.join(here, '..', '..', 'apps', 'sales', 'templates', 'sales', 'pos.html'), 'utf8');

/* Pull the named functions out of the template's <script> block. */
function extract(name) {
  const start = template.indexOf(`    function ${name}(`);
  if (start === -1) throw new Error(`function ${name} not found in pos.html`);
  let i = template.indexOf('{', start);
  let depth = 0;
  for (; i < template.length; i++) {
    if (template[i] === '{') depth++;
    else if (template[i] === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return template.slice(start, i);
}

const TIERS = ['RETAIL', 'WHOLESALE', 'DISTRIBUTOR'];
const TIER_LABELS = { RETAIL: 'Normal', WHOLESALE: 'Wholesale', DISTRIBUTOR: 'Distributor' };

const swalCalls = [];

// Evaluate the extracted source with `cart` as a real closure variable, so the
// functions behave exactly as they do in the page.
const run = new Function('TIERS', 'TIER_LABELS', 'Swal', 'renderCart', `
  let cart = [];
  ${extract('tierPrice')}
  ${extract('mergeDuplicateLines')}
  ${extract('toggleItemPrice')}
  return {
    tierPrice, mergeDuplicateLines, toggleItemPrice,
    getCart: () => cart, setCart: (c) => { cart = c; },
  };
`)(TIERS, TIER_LABELS, { fire: (o) => swalCalls.push(o) }, () => {});

const fails = [];
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? ' :: ' + extra : ''}`);
  if (!cond) fails.push(name);
};

const FULL = { RETAIL: 100, WHOLESALE: 90, DISTRIBUTOR: 80 };
const NO_DIST = { RETAIL: 12, WHOLESALE: 10, DISTRIBUTOR: null };
const RETAIL_ONLY = { RETAIL: 5, WHOLESALE: null, DISTRIBUTOR: null };

// ---------------------------------------------------------------- tierPrice
check('tierPrice reads each tier',
      run.tierPrice(FULL, 'RETAIL') === 100 &&
      run.tierPrice(FULL, 'WHOLESALE') === 90 &&
      run.tierPrice(FULL, 'DISTRIBUTOR') === 80);
check('tierPrice returns null for an unset tier', run.tierPrice(NO_DIST, 'DISTRIBUTOR') === null);
check('tierPrice returns null for an unknown tier', run.tierPrice(FULL, 'VIP') === null);
check('tierPrice handles a missing price map', run.tierPrice(undefined, 'RETAIL') === null);
check('tierPrice does not treat 0 as missing', run.tierPrice({ RETAIL: 0 }, 'RETAIL') === 0);

// ------------------------------------------------------- mergeDuplicateLines
run.setCart([
  { id: '1', tier: 'RETAIL', qty: 2, price: 100, name: 'A' },
  { id: '1', tier: 'RETAIL', qty: 3, price: 100, name: 'A' },
  { id: '1', tier: 'WHOLESALE', qty: 1, price: 90, name: 'A' },
  { id: '2', tier: 'RETAIL', qty: 4, price: 5, name: 'B' },
]);
run.mergeDuplicateLines();
let cart = run.getCart();
check('merges lines with the same product AND tier', cart.length === 3, `${cart.length} lines`);
check('merged quantity is summed', cart[0].qty === 5, String(cart[0].qty));
check('a different tier stays its own line',
      cart[1].tier === 'WHOLESALE' && cart[1].qty === 1);
check('an unrelated product is untouched', cart[2].id === '2' && cart[2].qty === 4);
check('line order is preserved', cart.map(i => i.id + i.tier).join() === '1RETAIL,1WHOLESALE,2RETAIL');

run.setCart([{ id: '1', tier: 'RETAIL', qty: 1 }]);
run.mergeDuplicateLines();
check('merging a single line is a no-op', run.getCart().length === 1);

run.setCart([]);
run.mergeDuplicateLines();
check('merging an empty cart is safe', run.getCart().length === 0);

// Three identical lines collapse into one
run.setCart([
  { id: '9', tier: 'DISTRIBUTOR', qty: 1 },
  { id: '9', tier: 'DISTRIBUTOR', qty: 2 },
  { id: '9', tier: 'DISTRIBUTOR', qty: 3 },
]);
run.mergeDuplicateLines();
check('three duplicates collapse to one line of 6',
      run.getCart().length === 1 && run.getCart()[0].qty === 6);

// ---------------------------------------------------------- toggleItemPrice
run.setCart([{ id: '1', name: 'Tyre', tier: 'RETAIL', price: 100, prices: FULL, qty: 1 }]);
run.toggleItemPrice(0);
check('cycles retail -> wholesale',
      run.getCart()[0].tier === 'WHOLESALE' && run.getCart()[0].price === 90);
run.toggleItemPrice(0);
check('cycles wholesale -> distributor',
      run.getCart()[0].tier === 'DISTRIBUTOR' && run.getCart()[0].price === 80);
run.toggleItemPrice(0);
check('wraps distributor -> retail',
      run.getCart()[0].tier === 'RETAIL' && run.getCart()[0].price === 100);

// Skips tiers the product has no price for
run.setCart([{ id: '2', name: 'Fluid', tier: 'RETAIL', price: 12, prices: NO_DIST, qty: 1 }]);
run.toggleItemPrice(0);
check('skips to wholesale when there is no distributor price',
      run.getCart()[0].tier === 'WHOLESALE' && run.getCart()[0].price === 10);
run.toggleItemPrice(0);
check('wraps straight back to retail, never landing on an unpriced tier',
      run.getCart()[0].tier === 'RETAIL' && run.getCart()[0].price === 12);

// A single-tier product cannot be switched
swalCalls.length = 0;
run.setCart([{ id: '3', name: 'Freshener', tier: 'RETAIL', price: 5, prices: RETAIL_ONLY, qty: 1 }]);
run.toggleItemPrice(0);
check('a retail-only product refuses to switch', run.getCart()[0].tier === 'RETAIL');
check('and explains why', swalCalls.length === 1 && /only one price/i.test(swalCalls[0].title || ''));

// Toggling onto an existing line merges them
run.setCart([
  { id: '1', name: 'Tyre', tier: 'RETAIL', price: 100, prices: FULL, qty: 2 },
  { id: '1', name: 'Tyre', tier: 'WHOLESALE', price: 90, prices: FULL, qty: 3 },
]);
run.toggleItemPrice(0);     // retail -> wholesale, now duplicates line 2
cart = run.getCart();
check('toggling onto an existing tier merges the lines',
      cart.length === 1 && cart[0].tier === 'WHOLESALE' && cart[0].qty === 5,
      JSON.stringify(cart.map(i => [i.tier, i.qty])));

// Out-of-range index is harmless
run.setCart([]);
run.toggleItemPrice(5);
check('toggling a non-existent line is safe', run.getCart().length === 0);

// ------------------------------------------------- template-level guarantees
check('the broken global wholesale checkbox is gone',
      !template.includes('wholesale-mode-check') && !template.includes('toggleGlobalWholesale'));
check('setGlobalTier re-prices the existing cart',
      /function setGlobalTier[\s\S]*?cart\.forEach\(item => \{[\s\S]*?item\.price = price;/.test(template));
check('the payload sends a tier per line',
      /cart\.map\(i => \(\{ id: i\.id, qty: i\.qty, tier: i\.tier, price: i\.price \}\)\)/.test(template));
check('the receipt is built from the server response',
      template.includes('result.lines || []') && template.includes('i.line_total.toFixed(2)'));
check('no stale isWholesale state remains', !template.includes('isWholesale'));

console.log(fails.length ? `\n${fails.length} FAILURES` : '\nALL PASS');
process.exit(fails.length ? 1 : 0);
