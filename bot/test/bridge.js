// Проверка моста к серверу (Bridge.gs) без Google и Telegram: node bot/test/bridge.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const store = { BRIDGE_SECRET: 's3cret' };
const props = {
  getProperty: k => (k in store ? store[k] : null), setProperty: (k, v) => { store[k] = String(v); }, deleteProperty: k => { delete store[k]; },
};
const sandbox = {
  console,
  PropertiesService: { getScriptProperties: () => props },
  ContentService: {
    MimeType: { JSON: 'json' },
    createTextOutput: text => ({ text, setMimeType() { return this; } }),
  },
};
vm.createContext(sandbox);
const code = ['Code.gs', 'Bridge.gs'].map(f => fs.readFileSync(path.join(__dirname, '..', f), 'utf8')).join('\n;\n');
vm.runInContext(code + ';globalThis.api = { doPost, pollUpdates, clientReply_ };', sandbox);
const { api } = sandbox;

let sent = [], writes = [];
sandbox.tgCall_ = (method, payload) => { if (method === 'sendMessage') sent.push(payload); if (method === 'getUpdates') throw new Error('polled'); return {}; };
sandbox.logEvent_ = () => {};
sandbox.ss_ = () => ({});
const person = (row, role, username, chatId) => ({ row, role, name: '', username, chatId: chatId || '', notify: true, aliases: [], generic: [], products: [] });
const team = {
  sheet: { getRange: (r, c) => ({ setValue: v => writes.push([r, c, v]) }) },
  idx: { chatId: 5 },
  people: [person(2, 'Маркетолог', 'lola_m', '777'), person(3, 'Продавцы', 'группа', '-100'), person(4, 'Дизайнер', '')],
};
sandbox.loadTeam_ = () => team;

let checks = 0;
const eq = (a, b) => { assert.deepStrictEqual(a, b); checks++; };
const post = body => JSON.parse(api.doPost({ postData: { contents: JSON.stringify(body) } }).text);
const msg = (text, from = {}, chat = { id: 555, type: 'private' }) => ({ text, from, chat });

// Чужой секрет — отказ, без ответа в Telegram
eq(post({ secret: 'nope', action: 'staff' }), { ok: false, error: 'forbidden' });
eq(api.doPost({ postData: { contents: '{not json' } }).text.includes('bad json'), true);

// Список сотрудников: username без «группа», все chat_id
eq(post({ secret: 's3cret', action: 'staff' }), { ok: true, usernames: ['lola_m'], chat_ids: ['777', '-100'] });

// Клиент: скрипт не отвечает сам, а говорит серверу «это клиент»
sent = [];
eq(post({ secret: 's3cret', action: 'message', message: msg('/start', { username: 'some_client' }) }), { ok: true, client: true });
eq(sent.length, 0);

// Сотрудник: подписка, как раньше, ответ уходит из скрипта
eq(post({ secret: 's3cret', action: 'message', message: msg('/start', { username: 'Lola_M' }, { id: 778, type: 'private' }) }), { ok: true, client: false });
eq(sent.length, 1);
eq(sent[0].text.includes('вы подписаны: Маркетолог'), true);
eq(JSON.stringify(writes), '[[2,6,"778"]]');

// После запроса флаг сброшен: без моста клиент снова получает приветствие
sent = [];
api.clientReply_(msg('hi'), false);
eq(sent.length, 1);

// Режим сервера: таблица не опрашивает Telegram
store.BRIDGE_MODE = '1';
api.pollUpdates();  // tgCall_('getUpdates') бросил бы ошибку
checks++;

console.log('OK: ' + checks + ' проверок моста');
