/* Offline DOM test for static/profit_report.js — the P&L filter controller.

   The regression this guards: the automatic / live refresh used to scroll
   the reader back to the top of the results ("it pulls me up while I am
   reading at the bottom").  A refresh must swap only the results region
   in place and restore the exact reading position — window scroll, inner
   table scroll, open <details> and focus.

   Run:  npm i jsdom   then   node tests/frontend/test_profit_report_dom.js
*/
const fs = require("fs");
const path = require("path");
let JSDOM;
try { JSDOM = require("jsdom").JSDOM; }
catch (_) { JSDOM = require("/tmp/node_modules/jsdom").JSDOM; }

const code = fs.readFileSync(
    path.join(__dirname, "..", "..", "static", "profit_report.js"),
    "utf8"
);

function resultsHtml(label) {
    return `<div id="plResults" data-pl-results data-pl-start="2026-03-01" data-pl-end="2026-03-31">
      <div class="ui-card" id="card1">
        <div class="pl-scroll" id="scroller1"><table><tbody>
          <tr><td>${label}</td></tr>
        </tbody></table></div>
        <a href="#focus" id="focusLink">focus me</a>
        <details id="det1"><summary>warnings</summary>details body</details>
      </div>
      <div class="ui-card" id="txCard">
        <table><tbody><tr><td>${label} transactions</td></tr></tbody></table>
        <nav><ul class="pagination"><li class="page-item">
          <a class="page-link" data-pl-nav href="/profit_reports?page=2">Next</a>
        </li></ul></nav>
      </div>
    </div>`;
}

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="plLoadingBar"></div>
  <span id="plFilterState"></span>
  <form id="plFilterForm" method="get" action="/profit_reports">
    <select name="period" data-pl-auto><option value="custom" selected>Custom</option></select>
    <input type="date" name="start_date" value="2026-03-01" data-pl-auto>
    <input type="date" name="end_date" value="" data-pl-auto>
  </form>
  <div id="plResultsRegion">${resultsHtml("old row")}</div>
</body></html>`, { url: "http://localhost/profit_reports", runScripts: "outside-only" });
const w = dom.window;
const doc = w.document;

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log("  ok  -", name); }
    else { fail++; console.log("  FAIL-", name, extra === undefined ? "" : "-> " + extra); }
}
function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms || 20));
}

/* The reader is deep in the report: 1800px down.  jsdom has no layout, so
   stub the viewport-relative rect of the transactions card (its top is
   900px above the viewport, i.e. 900px from the document top). */
Object.defineProperty(w, "pageYOffset", { value: 1800, configurable: true });
Object.defineProperty(w, "pageXOffset", { value: 0, configurable: true });
w.Element.prototype.getBoundingClientRect = function () {
    if (this.id === "txCard") {
        return { top: -900, bottom: -400, left: 0, right: 100, width: 100, height: 500, x: 0, y: -900 };
    }
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0 };
};
const scrollCalls = [];
w.scrollTo = function (arg) { scrollCalls.push(arg); };

const fetchUrls = [];
let fetchHandler = null;
w.fetch = function (url) {
    fetchUrls.push(url);
    return fetchHandler(url);
};
function deferredFetch(html) {
    let resolve;
    const promise = new Promise(function (res) { resolve = res; });
    fetchHandler = function () { return promise; };
    return function flush() {
        resolve({ ok: true, status: 200, text: () => Promise.resolve(html) });
    };
}
function immediateFetch(html) {
    fetchHandler = function () {
        return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(html) });
    };
}

w.eval(code);
check("script exposes FBMProfitFilters", !!w.FBMProfitFilters && typeof w.FBMProfitFilters.refresh === "function");
w.FBMProfitFilters.init({
    form: "#plFilterForm",
    region: "#plResultsRegion",
    partialParam: "partial",
    loadingBar: "#plLoadingBar",
    stateLabel: "#plFilterState",
    data: {}
});

(async function () {
    /* --- 1. background live refresh (ledger_live.js path) -------------- */
    scrollCalls.length = 0;
    const flushLive = deferredFetch(resultsHtml("new row"));
    const liveRefresh = w.FBMProfitFilters.refresh(null, { keepScroll: true, background: true });
    check("background refresh shows the progress bar",
        doc.getElementById("plLoadingBar").classList.contains("is-on"));
    check("background refresh does not dim the report under the reader",
        !doc.querySelector("[data-pl-results]").classList.contains("is-loading"));
    check("live refresh is a JS partial request, not a page reload",
        fetchUrls.length === 1 && /partial=1/.test(fetchUrls[0]), fetchUrls[0]);

    // The reader keeps interacting while the request is in flight; the
    // position must be captured at swap time, not at request time.
    doc.getElementById("scroller1").scrollTop = 140;
    doc.getElementById("det1").open = true;
    doc.getElementById("focusLink").focus();
    const oldSurface = doc.getElementById("plResults");
    flushLive();
    await liveRefresh;
    await wait();

    check("results were updated in place", doc.getElementById("plResults") !== oldSurface
        && /new row/.test(doc.getElementById("plResultsRegion").textContent));
    check("window scroll restored to the reader's position (never pulled up)",
        scrollCalls.length >= 1 && scrollCalls.every(call => call.top === 1800),
        JSON.stringify(scrollCalls));
    check("inner table scroll position preserved", doc.getElementById("scroller1").scrollTop === 140,
        doc.getElementById("scroller1").scrollTop);
    check("open <details> stays open", doc.getElementById("det1").open === true);
    check("focus inside the results is restored", doc.activeElement && doc.activeElement.id === "focusLink",
        doc.activeElement && doc.activeElement.id);

    /* --- 2. ordinary filter refresh also stays put ---------------------- */
    scrollCalls.length = 0;
    const flushFilter = deferredFetch(resultsHtml("filtered row"));
    const filterRefresh = w.FBMProfitFilters.refresh();
    check("filter refresh dims the results while loading",
        doc.querySelector("[data-pl-results]").classList.contains("is-loading"));
    flushFilter();
    await filterRefresh;
    await wait();
    check("filter refresh updated the figures", /filtered row/.test(doc.getElementById("plResultsRegion").textContent));
    check("filter refresh does not move the reader either",
        scrollCalls.length >= 1 && scrollCalls.every(call => call.top === 1800),
        JSON.stringify(scrollCalls));

    /* --- 3. explicit pagination lands on its own table, not the top ---- */
    scrollCalls.length = 0;
    immediateFetch(resultsHtml("page two row"));
    doc.querySelector("a.page-link[data-pl-nav]").click();
    await wait(40);
    check("pagination still fetches the partial", /page=2/.test(fetchUrls[fetchUrls.length - 1])
        && /partial=1/.test(fetchUrls[fetchUrls.length - 1]), fetchUrls[fetchUrls.length - 1]);
    check("pagination lands on the transactions card (900 - 12), not the report top",
        scrollCalls.length >= 1 && scrollCalls[scrollCalls.length - 1].top === 888,
        JSON.stringify(scrollCalls));

    /* --- 4. back/forward (popstate) never triggers a full reload ------- */
    const fetchesBefore = fetchUrls.length;
    w.dispatchEvent(new w.Event("popstate"));
    await wait();
    check("popstate on the same URL is a no-op (no reload, no refetch)",
        fetchUrls.length === fetchesBefore, fetchUrls.length);

    check("no full-page reload anywhere in the controller", !/location\.reload/.test(code));

    console.log(fail === 0 ? "\nALL " + pass + " CHECKS PASSED" : "\n" + fail + " FAILED / " + pass + " passed");
    process.exit(fail === 0 ? 0 : 1);
})();
