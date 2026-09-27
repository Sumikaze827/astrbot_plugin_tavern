const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../pages/console/app.js'), 'utf8');
const nodes = new Map();
const $ = (id) => {
  if (!nodes.has(id)) nodes.set(id, {value: '', innerHTML: '', addEventListener(type, fn) {this[type] = fn;}});
  return nodes.get(id);
};
let editor, posted, refreshed = false;
const cards = [
  {id:'p1', source_session_id:'s1', instance_name:'原本一', character_name:'甲', display_name:'玩家甲', group_user_id:'u1'},
  {id:'p2', source_session_id:'s2', instance_name:'原本二', character_name:'乙', display_name:'玩家乙', group_user_id:'u2'},
];
const ctx = vm.createContext({
  app: {sessions:[{id:'target',instance_name:'新本'}]}, $, escapeHTML: String,
  bridge: {apiGet: async () => ({items: cards}), apiPost: async (route, data) => {posted = {route, data}; return {participant:{auto_approved:true}};}},
  openEditor: (config) => {editor = config; $('#card-import-story').value = 's1';},
  toast: () => {}, loadSessionPage: async () => {refreshed = true;},
});
vm.runInContext(source.slice(source.indexOf('function sessionActions('), source.indexOf('function sessionCardMarkup(')), ctx);
vm.runInContext(source.slice(source.indexOf('async function runSessionAction('), source.indexOf('async function openSessionDetail(')), ctx);
(async () => {
  assert(ctx.sessionActions({state:'preparing',turn_no:0}).some(([key]) => key === 'card-import'));
  for (const state of ['running','finished','paused']) {
    assert(!ctx.sessionActions({state,turn_no:0}).some(([key]) => key === 'card-import'));
  }
  assert(!ctx.sessionActions({state:'preparing',turn_no:9}).some(([key]) => key === 'card-import'));
  await ctx.runSessionAction('target', 'card-import');
  assert(editor.title.includes('新本'));
  assert($('#card-import-player').innerHTML.includes('p1'));
  assert(!$('#card-import-player').innerHTML.includes('p2'));
  $('#card-import-story').value = 's2';
  $('#card-import-story').change();
  assert($('#card-import-player').innerHTML.includes('p2'));
  $('#card-import-player').value = 'p2';
  await editor.onSave();
  assert.equal(posted.route, 'sessions/card-import');
  assert.equal(posted.data.session_id, 'target');
  assert.equal(posted.data.source_participant_id, 'p2');
  assert(refreshed);
  console.log('card import UI: button visibility, source selection, submission and refresh passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
