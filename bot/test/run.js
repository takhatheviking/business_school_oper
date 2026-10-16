// Проверка логики бота без Google и Telegram, на вымышленных данных: node bot/test/run.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const store = {};
const props = {
  getProperty: k => (k in store ? store[k] : null), setProperty: (k, v) => { store[k] = v; }, deleteProperty: k => { delete store[k]; },
};
const sandbox = { console, PropertiesService: { getScriptProperties: () => props } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'Code.gs'), 'utf8') +
  ';globalThis.api = { makeContext_, buildDigest_, buildReminders_, buildStatusEvents_, matchPeople_, splitList_, handleMessage_, CONFIG };', sandbox);
const { api } = sandbox;

const person = (row, role, o = {}) => ({
  row, role, name: o.name || '', dept: '', aliases: api.splitList_(o.aliases), generic: api.splitList_(o.generic),
  products: api.splitList_(o.products), username: o.username || '', chatId: o.chatId || '', notify: o.notify !== false,
});
const people = [
  person(2, 'Руководитель', { name: 'Анвар', aliases: 'анвара' }),
  person(3, 'Маркетолог', { name: 'Лола' }),
  person(4, 'Монтажёр'),
  person(5, 'Монтажёр №2'),
  person(6, 'Методист BMC', { generic: 'методист', products: 'BMC' }),
  person(7, 'Методист PM', { generic: 'методист', products: 'PM' }),
  person(8, 'Ассистент', { notify: false }),
];
let row = 2;
const task = (id, due, dept, product, r, o = {}) => Object.assign({
  row: row++, id, week: 'Н1 (19.10–25.10)', due, dept, product, task: 'Задача ' + id, r, status: 'Не начато',
  comment: '', result: 'Критерий ' + id, a: '', c: '', i: '', fact: '',
}, o);
const tasks = [
  task('A-1', '2026-10-25', 'Офис', 'Школа', 'Анвар'),
  task('A-2', '2026-10-25', 'Офис', 'Школа', 'Ассистент + Монтажёр №2'),
  task('M-1', '2026-10-25', 'Маркетинг', 'BMC', 'Маркетолог'),
  task('M-2', '2026-10-18', 'Маркетинг', 'BMC', 'Монтажёр'),
  task('E-1', '2026-10-25', 'Обучение', 'PM', 'Методист + Анвар'),
  task('E-2', '2026-10-25', 'Обучение', 'BMC', 'Методист', { status: 'Готово' }),
  task('E-3', '2026-10-18', 'Обучение', 'PM', 'Claude, проверка Анвара'),
];
const ctx = api.makeContext_({
  tasks, people, defaultA: { 'Офис': 'Анвар', 'Маркетинг': 'Лола', 'Обучение': 'Анвар' },
  url: 'https://example.com/edit', gid: 1,
});
const roles = (text, product) => api.matchPeople_(text, product, ctx.matcher).people.map(p => p.role).sort().join(' | ');
let checks = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); checks++; };
const eq = (a, b) => { assert.strictEqual(a, b); checks++; };

// Сопоставление ролей
eq(roles('Монтажёр №2', 'BMC'), 'Монтажёр №2');                         // длинное название раньше короткого
eq(roles('Монтажер + Монтажёр №2', 'BMC'), 'Монтажёр | Монтажёр №2');    // ё = е
eq(roles('Методист', 'PM'), 'Методист PM');                             // общее название — по продукту задачи
eq(roles('Методист', 'Школа'), 'Методист BMC | Методист PM');           // продукт не подошёл — всем
eq(roles('Claude, проверка Анвара', 'PM'), 'Руководитель');             // падеж через «Другие названия»
eq(api.matchPeople_('Анвар + комиссия', 'BMC', ctx.matcher).rest, 'комиссия'); // нераспознанное видно в проверке

// Понедельничный дайджест 19.10
const digest = api.buildDigest_(ctx, '2026-10-19');
const forRole = r => digest.find(d => d.people.some(p => p.role === r));
ok(forRole('Монтажёр').text.includes('Просрочено'), 'исполнитель видит просрочку');
ok(forRole('Методист BMC').text.includes('уже закрыты'), 'закрытая неделя');
ok(forRole('Маркетолог').text.includes('Просрочено у исполнителей: 1'), 'утверждающий видит чужую просрочку');
const lead = forRole('Руководитель').text;
ok(lead.includes('A-1') && lead.includes('E-3') && lead.includes('Вы утверждаете'), 'свои задачи и раздел утверждающего');
ok(!/Вы утверждаете[\s\S]*E-1/.test(lead), 'свою задачу человек не утверждает сам у себя');

// Напоминание в пятницу 16.10 о сроке 18.10
const reminders = api.buildReminders_(ctx, '2026-10-16');
eq(reminders.length, 2);
ok(reminders.every(r => r.text.includes('До срока 2 дня')), 'текст напоминания');

// Смена статусов
const byId = id => tasks.find(t => t.id === id);
byId('A-2').status = 'Готово';
byId('E-1').status = 'Готово';
byId('M-2').status = 'Риск';
byId('M-2').comment = 'Ждём исходники';
const events = api.buildStatusEvents_(ctx, [byId('A-2'), byId('E-1'), byId('M-2')], props);
eq(events.map(e => e.kind + '→' + e.person.role).sort().join(', '), 'на приёмку→Руководитель, риск→Маркетолог');
ok(events.find(e => e.kind === 'риск').text.includes('Ждём исходники'), 'комментарий в сообщении о риске');
eq(api.buildStatusEvents_(ctx, [byId('A-2')], props).length, 0);         // тот же статус второй раз не шлём

// Входящие сообщения: бот общий для команды и клиентов
let sent = [], calls = [], logs = [], writes = [];
sandbox.tgCall_ = (method, payload) => { calls.push({ method, payload }); if (method === 'sendMessage') sent.push(payload); return {}; };
sandbox.logEvent_ = (...a) => logs.push(a);
sandbox.loadContext_ = () => { throw new Error('клиенту не нужны задачи'); };
const team = {
  sheet: { getRange: (r, c) => ({ setValue: v => writes.push([r, c, v]) }) },
  idx: { chatId: 5 },
  people: [
    person(2, 'Маркетолог', { username: 'lola_m' }),
    person(3, 'Продавцы', { username: 'группа' }),
  ],
};
const say = (text, from = {}, chat = { id: 555, type: 'private' }) => {
  sent = []; calls = []; logs = []; writes = [];
  api.handleMessage_({ text, from, chat }, team);
  return sent.map(s => s.text).join('\n---\n');
};
const greeting = api.CONFIG.CLIENT_GREETING;
const client = { username: 'Some_Client', first_name: 'Азиз' };

let out = say('/start', client);
eq(out, greeting);                                                         // клиенту — только приветствие школы
ok(!/команд|chat_id|555/i.test(out), 'клиент не видит служебного');
eq(logs[0].slice(0, 2).join(' '), 'клиент @Some_Client');                 // но в журнале он есть
eq(say('/start', { first_name: 'Без username' }), greeting);              // без username — то же приветствие
eq(say('Сколько стоит курс?', client), greeting);
eq(say('/my', client), greeting);                                         // служебные команды клиенту не отвечают
eq(say('/help', client), greeting);
eq(say('/test', client), greeting);
ok(!/555/.test(say('/team', client)), 'скрытая /team не выдаёт chat_id тому, у кого есть username');

store.TEST_ARM_UNTIL = String(Date.now() + 60000);
store.TEST_CODE = '424242';
eq(say('/test 111111', client), greeting);                                 // чужой код — не тестовый чат
eq(store.TEST_CHAT_ID, undefined);

out = say('/start', { username: 'Lola_M' }, { id: 777, type: 'private' });
ok(out.includes('вы подписаны: Маркетолог'), 'сотрудник подписывается по username');
eq(JSON.stringify(writes), '[[2,6,"777"]]');                               // chat_id записан в его строку
ok(calls.some(c => c.method === 'setMyCommands' && c.payload.scope.chat_id === '777'), 'у сотрудника своё меню');
ok(say('привет', {}, { id: 777, type: 'private' }).includes('/my'), 'подписанного узнаёт по chat_id');
ok(say('/test 424242', { username: 'lola_m' }, { id: 777, type: 'private' }).includes('Тестовый режим включён'), 'тест с верным кодом');
eq(store.TEST_CHAT_ID, '777');
eq(store.TEST_CODE, undefined);                                           // код одноразовый

ok(say('/start Продавцы', {}, { id: -100, type: 'group' }).includes('чат подписан: Продавцы'), 'общий чат роли');
ok(say('/start Маркетолог', {}, { id: -100, type: 'group' }).includes('Не могу подписать'), 'чат — только ролям «группа»');
eq(say('всем привет', {}, { id: -100, type: 'group' }), '');                // в группе на обычный текст молчит

console.log('OK: ' + checks + ' проверок');
