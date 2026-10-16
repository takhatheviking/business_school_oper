// Проверка логики бота без Google и Telegram, на вымышленных данных: node bot/test/run.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'Code.gs'), 'utf8') +
  ';globalThis.api = { makeContext_, buildDigest_, buildReminders_, buildStatusEvents_, matchPeople_, splitList_ };', sandbox);
const { api } = sandbox;

const person = (row, role, o = {}) => ({
  row, role, name: o.name || '', dept: '', aliases: api.splitList_(o.aliases), generic: api.splitList_(o.generic),
  products: api.splitList_(o.products), username: '', chatId: '', notify: o.notify !== false,
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
const store = {};
const props = { getProperty: k => (k in store ? store[k] : null), setProperty: (k, v) => { store[k] = v; } };
const byId = id => tasks.find(t => t.id === id);
byId('A-2').status = 'Готово';
byId('E-1').status = 'Готово';
byId('M-2').status = 'Риск';
byId('M-2').comment = 'Ждём исходники';
const events = api.buildStatusEvents_(ctx, [byId('A-2'), byId('E-1'), byId('M-2')], props);
eq(events.map(e => e.kind + '→' + e.person.role).sort().join(', '), 'на приёмку→Руководитель, риск→Маркетолог');
ok(events.find(e => e.kind === 'риск').text.includes('Ждём исходники'), 'комментарий в сообщении о риске');
eq(api.buildStatusEvents_(ctx, [byId('A-2')], props).length, 0);         // тот же статус второй раз не шлём

console.log('OK: ' + checks + ' проверок');
