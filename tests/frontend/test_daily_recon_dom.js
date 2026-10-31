/* Run with jsdom installed: node tests/frontend/test_daily_recon_dom.js */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const template = fs.readFileSync(path.join(__dirname, '../../templates/_daily_recon_board.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const dom = new JSDOM(`<!doctype html><div data-dr-row="1">
  <span data-dr-save-state></span>
  <form action="/daily_reconciliation" data-dr-count-form>
    <input name="action" value="save_count"><input name="account_id" value="1">
    <input name="day" value="2026-10-31"><input name="counted" value="39,610">
    <input name="_csrf_token" value="token"><button type="submit" name="action" value="save_count">Save</button>
  </form>
  <form action="/daily_reconciliation" data-dr-clear-form>
    <input name="action" value="clear_count"><input name="account_id" value="1">
    <input name="day" value="2026-10-31"><input name="_csrf_token" value="token">
    <button type="submit" name="action" value="clear_count">Clear</button>
  </form>
</div>`, { url: 'https://example.test/cash_flow', runScripts: 'outside-only' });
const w = dom.window;
const forms = [...w.document.querySelectorAll('form')];
// Browsers expose named controls on forms; emulate the action property being
// shadowed explicitly (DOM implementations differ in named-property support).
for (const form of forms) {
  Object.defineProperty(form, 'action', { value: form.querySelector('input[name="action"]') });
}
const requests = [];
w.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ok: true}) };
};
w.eval(script);

(async () => {
  for (const [i, action] of ['save_count', 'clear_count'].entries()) {
    let prevented = false;
    const ev = new w.SubmitEvent('submit', { bubbles: true, cancelable: true, submitter: forms[i].querySelector('button') });
    forms[i].dispatchEvent(ev);
    prevented = ev.defaultPrevented;
    await new Promise(resolve => setImmediate(resolve));
    assert(prevented);
    assert.strictEqual(requests[i].url, '/daily_reconciliation');
    assert.strictEqual(requests[i].options.method, 'POST');
    assert.strictEqual(requests[i].options.body.get('action'), action);
    assert.strictEqual(requests[i].options.body.get('_csrf_token'), 'token');
    assert.strictEqual(w.document.querySelector('[data-dr-save-state]').textContent, 'Saved');
  }
  assert.strictEqual(requests[0].options.body.get('counted'), '39,610');
  dom.window.close();
  console.log('daily reconciliation save and clear DOM tests passed');
})().catch(err => { console.error(err); dom.window.close(); process.exitCode = 1; });
