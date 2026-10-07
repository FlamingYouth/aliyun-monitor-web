'use strict';
// Dependency-free regression checks for the real front-end handler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\nboot\(\);\s*$/, '\n');
const handlers = {}, nodes = {}, requests = [];
function node(extra = {}) {
  return {hidden: false, textContent: '', className: '', addEventListener() {},
    classList: {remove() {}, toggle() {}}, ...extra};
}
for (const name of ['loading', 'app', 'onboarding', 'mobile-menu', 'test-result', 'page-label', 'navigation']) nodes['#' + name] = node();
nodes['.sidebar'] = node();
nodes['#onboarding'].replaceChildren = () => {nodes['#wizard-form'] = null;};
class TestFormData {
  constructor(form) {this.form = form;}
  [Symbol.iterator]() {return Object.entries(this.form.data)[Symbol.iterator]();}
}
const context = vm.createContext({
  document: {querySelector: s => nodes[s] || null, addEventListener: (kind, fn) => {handlers[kind] = fn;}},
  window: {addEventListener() {}}, location: {hash: ''},
  FormData: TestFormData, setInterval() {}, setTimeout() {}, console,
  fetch: async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => url === '/api/status'
      ? {csrf: 'synthetic-csrf', authenticated: true, setup_required: false}
      : {message: '发送成功'}};
  }
});
vm.runInContext(code, context);
vm.runInContext('refresh = async () => {};', context);
function form(id) {
  return {id, data: {wecom_url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-test-only'},
    reportValidity: () => true, querySelectorAll: () => []};
}
async function clickTest(actualForm) {
  const button = {dataset: {action: 'test-wecom', channel: 'wecom'}, disabled: false, closest: () => actualForm};
  await handlers.click({target: {closest: () => button}});
  assert.equal(button.disabled, false);
  return requests.at(-1);
}
(async () => {
  // Reproduce the bug: wizard still exists, but clicked button belongs to notification form.
  nodes['#wizard-form'] = form('wizard-form');
  nodes['#onboarding'].hidden = true;
  const regular = await clickTest(form('notification-form'));
  assert.equal(regular.url, '/api/notifications/test');
  assert.equal(regular.options.headers['X-Setup-Token'], undefined);
  assert.equal(JSON.parse(regular.options.body).url.includes('synthetic-test-only'), true);
  assert.equal(nodes['#test-result'].textContent, '发送成功');
  // Wizard is valid only while onboarding is actually visible.
  nodes['#onboarding'].hidden = false;
  vm.runInContext("step = 3; setupToken = 'synthetic-setup';", context);
  const wizard = await clickTest(nodes['#wizard-form']);
  assert.equal(wizard.url, '/api/setup/test');
  assert.equal(wizard.options.headers['X-Setup-Token'], 'synthetic-setup');
  // Authenticated boot actively removes stale wizard instead of merely hiding it.
  await vm.runInContext('boot()', context);
  assert.equal(nodes['#wizard-form'], null);
  assert.equal(nodes['#onboarding'].hidden, true);
  assert.equal(nodes['#app'].hidden, false);
  const afterBoot = await clickTest(form('notification-form'));
  assert.equal(afterBoot.url, '/api/notifications/test');
  assert.equal(afterBoot.options.headers['X-CSRF-Token'], 'synthetic-csrf');
  // A single valid sample must produce a visible marker, with invalid readings excluded.
  const samples = vm.runInContext('validSamples([{at:1,traffic:0},{at:2,traffic:null},{at:3,traffic:"bad"}])', context);
  assert.equal(samples.length, 1);
  context.chartSamples = samples;
  const svg = vm.runInContext('trafficSVG(chartSamples)', context);
  assert.ok(svg.includes('<circle'));
  assert.ok(svg.includes('每日 CDT 流量折线图'));
  assert.equal(svg.includes('NaN'), false);
  // The highest real reading defines the traffic axis, even if usage later falls.
  const scaledTraffic = vm.runInContext('trafficSVG([{at:1790784000,traffic:2},{at:1790870400,traffic:1}])', context);
  assert.ok(scaledTraffic.includes('>2.00</text>'));
  assert.ok(scaledTraffic.includes('>1.00</text>'));
  assert.equal(scaledTraffic.includes('198.00'), false);
  assert.equal(scaledTraffic.includes('stroke-dasharray'), false);
  assert.ok(scaledTraffic.includes('data-chart-label='));
  assert.ok(scaledTraffic.includes('当日 CDT 流量：2.00 GB'));
  // Daily flow is derived from adjacent calendar-day totals; the toolbar keeps MTD.
  // Clearly synthetic totals for the public regression fixture.
  const cumulative = [0,2.25,5.75,7,10.25,12.25,16.25].map((traffic,index)=>({at:Date.UTC(2026,9,index+1)/1000,traffic}));
  context.cumulative = cumulative;
  const daily = vm.runInContext("dailyTraffic(cumulative,'2026-10')", context);
  assert.deepEqual(Array.from(daily,s=>s.traffic), [0,2.25,3.50,1.25,3.25,2,4]);
  assert.equal(Math.max(...Array.from(daily,s=>s.traffic)), 4);
  const newest = {...cumulative.at(-1),at:Date.UTC(2026,9,7,12)/1000,traffic:17.75};
  context.latestCumulative = [...cumulative,newest];
  assert.equal(vm.runInContext("dailyTraffic(latestCumulative,'2026-10').at(-1).traffic", context), 5.5);
  nodes['#chart'] = node({innerHTML:'',clientWidth:420});
  vm.runInContext("dashboardCharts={month:'2026-10',today:'2026-10-07',accounts:[{id:'daily',name:'Daily',samples:cumulative,threshold:180}]};trafficAccount='';renderTrafficChart();", context);
  assert.ok(nodes['#chart'].innerHTML.includes('本月累计'));
  assert.ok(nodes['#chart'].innerHTML.includes('16.25 GB'));
  const dailyPlot = nodes['#chart'].innerHTML.match(/<svg class="data-chart traffic-chart"[\s\S]*?<\/svg>/)[0];
  assert.ok(dailyPlot.includes('>4.00</text>'));
  assert.ok(dailyPlot.includes('>3.50</text>'));
  assert.equal(dailyPlot.includes('16.25'), false);
  assert.equal(dailyPlot.includes('180'), false);
  // Missing days must not turn several days of traffic into one day's usage.
  context.gappedTotals = [cumulative[0],{...cumulative[2],traffic:3},{...cumulative[3],traffic:7}];
  const gappedDaily = vm.runInContext("dailyTraffic(gappedTotals,'2026-10')", context);
  assert.deepEqual(Array.from(gappedDaily,s=>s.traffic),[0,null,4]);
  context.gappedDaily=gappedDaily;
  const gapPlot = vm.runInContext('trafficSVG(validSamples(gappedDaily))',context);
  const gapLine = gapPlot.match(/<path d="([^"]+)" stroke="#0e887d"/)[1];
  assert.equal((gapLine.match(/M/g)||[]).length,2);
  assert.equal(gapLine.includes('L'),false);
  context.newMonthTotals=[{at:Date.UTC(2026,8,30)/1000,traffic:70},{at:Date.UTC(2026,9,1)/1000,traffic:2},{at:Date.UTC(2026,9,2)/1000,traffic:3}];
  assert.deepEqual(Array.from(vm.runInContext("dailyTraffic(newMonthTotals,'2026-10')",context),s=>s.traffic),[2,1]);
  context.correctedTotals=[{at:Date.UTC(2026,9,1)/1000,traffic:5},{at:Date.UTC(2026,9,2)/1000,traffic:3}];
  assert.deepEqual(Array.from(vm.runInContext("dailyTraffic(correctedTotals,'2026-10')",context),s=>s.traffic),[5,null]);
  // Unknown daily amounts are marked as pending, rather than a fabricated zero bill.
  const bars = vm.runInContext("billingSVG([{day:'2026-10-01',amount:0,currency:'CNY'}],'2026-10',2)", context);
  assert.ok(bars.includes('2026-10-01 · CNY 0.00'));
  assert.ok(bars.includes('2026-10-02 · 待出账或待查询'));
  assert.ok(bars.includes('柱状图'));
  const smallBills = vm.runInContext("billingSVG([{day:'2026-10-01',amount:0.02,currency:'USD'}],'2026-10',1)", context);
  const barHeight = Number(smallBills.match(/<rect class="chart-bar"[^>]*height="([^"]+)"/)[1]);
  assert.ok(barHeight > 150, 'Small daily fees must be visible using their actual scale');
  assert.ok(smallBills.includes('0.02</text>'));
  assert.ok(smallBills.includes('class="chart-value"'));
  assert.ok(smallBills.includes('每日费用：USD 0.02'));
  // Truncate bill amounts and use those same values for bar heights and tooltips.
  const preciseBills = vm.runInContext("billingSVG([{day:'2026-10-01',amount:0.02468,currency:'USD'},{day:'2026-10-02',amount:0.0156789,currency:'USD'}],'2026-10',2,300)", context);
  assert.ok(preciseBills.includes('>0.02</text>'));
  assert.ok(preciseBills.includes('>0.01</text>'));
  assert.ok(preciseBills.includes('每日费用：USD 0.02'));
  assert.ok(preciseBills.includes('每日费用：USD 0.01'));
  const preciseHeights = [...preciseBills.matchAll(/<rect class="chart-bar"[^>]*height="([^"]+)"/g)].map(match => Number(match[1]));
  assert.ok(Math.abs(preciseHeights[0]/preciseHeights[1]-2) < 1e-10);
  context.sameDisplayedBills=[{day:'2026-10-01',amount:0.02468,currency:'USD'},{day:'2026-10-02',amount:0.02999,currency:'USD'}];
  const equalBars=vm.runInContext("billingSVG(sameDisplayedBills,'2026-10',2)",context);
  const equalHeights=[...equalBars.matchAll(/<rect class="chart-bar"[^>]*height="([^"]+)"/g)].map(match=>Number(match[1]));
  assert.equal(equalHeights[0],equalHeights[1]);
  assert.equal(context.sameDisplayedBills[0].amount,0.02468,'Formatting must preserve the raw bill data');
  const billTop=Number(equalBars.match(/<rect class="chart-bar"[^>]*y="([^"]+)"/)[1]);
  const maxTick=Number(equalBars.match(/<text x="[^"]+" y="([^"]+)" text-anchor="end">0.02<\/text>/)[1]);
  assert.ok(Math.abs(maxTick-4-billTop)<1e-10,'Axis labels must align with the plotted amount');
  for(const [value,expected] of [[0,'0.00'],[0.024680000000000003,'0.02'],[0.01234,'0.01'],[0.01999,'0.01'],[1.15,'1.15'],[1.99999,'1.99'],[1.1499999,'1.14'],[1e-7,'0.00'],[1.239e2,'123.90'],[1e21,'1000000000000000000000.00'],[-0.01999,'-0.01'],[-0.009,'0.00']]){
    context.truncationValue=value;
    assert.equal(vm.runInContext('billNumber(truncationValue)',context),expected);
  }
  assert.equal(vm.runInContext("billMoney({amount:0.13999,currency:'USD'})",context),'USD 0.13');
  const refund = vm.runInContext("billingSVG([{day:'2026-10-01',amount:-0.01,currency:'USD'},{day:'2026-10-02',amount:null,currency:'USD'}],'2026-10',2)", context);
  assert.ok(refund.includes('>-0.01</text>'));
  assert.ok(refund.includes('2026-10-02 · 待出账或待查询'));
  assert.equal(refund.includes('NaN'), false);
  // Pointer and keyboard interactions show the exact date and amount, then dismiss.
  const tooltip = node({hidden:true});
  const area = {querySelector: () => tooltip};
  const child = {};
  const mark = {dataset:{chartLabel:'2026-10-07\n每日费用：USD 0.02'},closest: () => area,contains: target => target===child};
  const target = {closest: () => mark};
  handlers.pointerover({target});
  assert.equal(tooltip.hidden, false);
  assert.equal(tooltip.textContent, '2026-10-07\n每日费用：USD 0.02');
  handlers.pointerout({target,relatedTarget:child});
  assert.equal(tooltip.hidden, false);
  handlers.pointerout({target,relatedTarget:null});
  assert.equal(tooltip.hidden, true);
  handlers.focusin({target});
  assert.equal(tooltip.hidden, false);
  handlers.focusout({target});
  assert.equal(tooltip.hidden, true);
  // Account summaries display current-month bills separately by currency.
  vm.runInContext("state={settings:{timezone:'Asia/Shanghai'},report_month:'2026-10',accounts:[{id:'usd',name:'International',site:'international',balance:{amount:8,currency:'USD'},bill:{amount:0.13999,currency:'USD'},bill_at:1791331200},{id:'cny',name:'China',site:'china',bill:{amount:40,currency:'CNY'},bill_at:1788192000}]};", context);
  const summary = vm.runInContext('accountSummary()', context);
  assert.ok(summary.includes('本月账号账单'));
  assert.ok(summary.includes('USD 0.13'));
  assert.equal(summary.includes('CNY 40.00'), false);
  assert.ok(summary.includes('待查询'));
  const updatedSummary = vm.runInContext("accountSummary([{id:'usd',bill:null},{id:'cny',bill:{amount:1.23999,currency:'CNY'}}])", context);
  assert.ok(updatedSummary.includes('CNY 1.23'));
  assert.equal(updatedSummary.includes('USD 0.13'), false);
  nodes['#bill-chart']=node({clientWidth:420});
  vm.runInContext("dashboardCharts={month:'2026-10',today:'2026-10-07',accounts:[{id:'usd',name:'International',site:'international',bill:{amount:0.13999,currency:'USD'},bills:[]}]};renderBillChart();",context);
  assert.ok(nodes['#bill-chart'].innerHTML.includes('<b>USD 0.13</b>'));
  nodes['#main']=node();
  vm.runInContext("state.instances=[];page='accounts';",context);
  await vm.runInContext('renderPage()',context);
  assert.ok(nodes['#main'].innerHTML.includes('本月账号账单</span><b>USD 0.13</b>'));
  const cards = vm.runInContext("notificationFields({wecom_enabled:true,bark_enabled:true,telegram_enabled:true,telegram_chat_id:'1234'})", context);
  for (const channel of ['wecom','bark','telegram']) {
    assert.ok(cards.includes('data-notification-channel="' + channel + '"'));
    assert.ok(cards.includes('data-channel="' + channel + '"'));
  }
  assert.equal(cards.includes('type="radio"'), false);
  // Modal tests opened by the setup wizard must still use its protected endpoint.
  nodes['#wizard-form'] = form('wizard-form');
  nodes['#onboarding'].hidden = false;
  const configForm = {...form('notification-form'), dataset: {wizard: 'true'}};
  assert.equal((await clickTest(configForm)).url, '/api/setup/test');
  // A response for an old dashboard must not replace a newer view after navigation.
  nodes['#chart'] = node({innerHTML:'old chart'});
  const oldChart = nodes['#chart'];
  let completeCharts;
  context.chartResponse = new Promise(resolve => {completeCharts = resolve;});
  vm.runInContext("page='dashboard';api=()=>chartResponse;", context);
  const pending = vm.runInContext('renderCharts()', context);
  nodes['#chart'] = node({innerHTML:'new chart'});
  completeCharts({accounts: [], month:'2026-10', today:'2026-10-07'});
  await pending;
  assert.equal(oldChart.innerHTML, 'old chart');
  assert.equal(nodes['#chart'].innerHTML, 'new chart');
  console.log('Front-end regression PASS: daily traffic differences / month-to-date total / missing and corrected samples / actual daily maximum / bill truncation and matching bar heights / pointer and keyboard feedback / account bills / notifications / stale chart response');
})().catch(error => {console.error(error); process.exitCode = 1;});
