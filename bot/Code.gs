/**
 * Proactive · Telegram-бот уведомлений по статусной таблице задач.
 * Этап 1: исполнители (R) и утверждающие (A).
 *
 * Что делает:
 *  • Пн в 9:00 — каждому исполнителю его задачи на неделю и просроченные;
 *    каждому утверждающему — сводка: сколько задач на неделе, что просрочено, что в «Риске».
 *  • За 2 дня до срока (пт для сроков в вс) — напоминание исполнителю о незакрытых задачах.
 *  • Статус сменили на «Готово» или «Риск» — сразу сообщение утверждающему;
 *    на «Отменено» — исполнителю.
 *  • /start в боте — сотрудник подписывается: бот находит его @username на листе «Команда».
 *
 * Бот общий для команды и клиентов (@proactive_school_bot). Всем, кого нет на листе «Команда»,
 * он отвечает приветствием школы (CLIENT_GREETING) и ничего не рассказывает о внутренней кухне.
 *
 * Как установить — README.md рядом с этим файлом.
 */

const CONFIG = {
  // Задачи ведутся на листах отделов: бот читает каждый лист, где в первой строке есть «ID», «Статус»,
  // «Исполнитель (R)» и «Срок». Общий лист собирается из них формулой — его бот пропускает.
  ALL_TASKS_SHEET: 'Все задачи',
  TEAM_SHEET: 'Команда',
  REF_SHEET: 'Справочник',
  LOG_SHEET: 'Журнал бота',
  PREVIEW_SHEET: 'Бот · предпросмотр',
  DAILY_HOUR: 9,            // час ежедневной рассылки (часовой пояс таблицы)
  DIGEST_WEEKDAY: 1,        // 1 = понедельник: недельный дайджест
  REMIND_DAYS_BEFORE: 2,    // напоминание за столько дней до срока
  POLL_MINUTES: 5,          // как часто бот читает входящие (/start, /my)
  MAX_ITEMS: 12,            // максимум задач недели в одном сообщении
  OVERDUE_MAX: 10,          // максимум просроченных задач в списке исполнителя
  APPROVER_TOP: 5,          // утверждающему: сколько самых давних просрочек показать списком
  TASK_TEXT_MAX: 120,       // обрезать текст задачи до стольких символов
  RESULT_TEXT_MAX: 110,     // обрезать критерий готовности
  IGNORE_WORDS: ['проверка', 'на встречах'],  // слова в «Исполнителе», которые не роли
  // Ответ всем, кого нет на листе «Команда», — то есть клиентам.
  CLIENT_GREETING: 'Здравствуйте! Это бот бизнес-школы Proactive.\n\n' +
    'Скоро здесь можно будет узнать о наших курсах и тренингах, записаться и получить билет.',
  CLIENT_CONTACT: '',       // например: 'Вопросы — @proactive_manager или +998 …'; пусто — строка не добавляется
  TEST_CODE_MINUTES: 10,    // сколько действует код для /test
  TEST_MAX_TRIES: 5,        // после стольких неверных кодов окно закрывается, чтобы код нельзя было подобрать
};

const STATUS = { NEW: 'Не начато', WORK: 'В работе', DONE: 'Готово', RISK: 'Риск', CANCELLED: 'Отменено' };

// Заголовки столбцов: бот ищет столбцы по названию, порядок можно менять.
const COL = {
  id: 'ID', week: 'Неделя', start: 'Начало', due: 'Срок', dept: 'Отдел', product: 'Продукт', task: 'Задача',
  r: 'Исполнитель (R)', status: 'Статус', fact: 'Дата факт', comment: 'Комментарий',
  result: 'Результат (критерий готовности)', a: 'Утверждает (A)', c: 'Консультирует (C)', i: 'Информировать (I)',
};
const TEAM = {
  role: 'Роль (как в задачах)', name: 'Имя', dept: 'Отдел', aliases: 'Другие названия в задачах',
  username: 'Telegram @username', chatId: 'Telegram chat_id', notify: 'Уведомлять',
  generic: 'Общее название (по продукту)', products: 'Продукты',
};

const STAFF_HELP = [
  'Команды для сотрудников:',
  '',
  '/my — мои задачи на этой неделе и просроченные',
  '/team — подписаться заново (например, если сменили username)',
  '/help — эта подсказка',
].join('\n');

// Меню команд: клиенты видят только эти две, у подписанных сотрудников — своё меню.
const CLIENT_COMMANDS = [{ command: 'start', description: 'Начать' }, { command: 'help', description: 'Помощь' }];
const STAFF_COMMANDS = [
  { command: 'my', description: 'Мои задачи сейчас' },
  { command: 'team', description: 'Подписаться заново' },
  { command: 'help', description: 'Помощь' },
];

/* ───────────────────────────── Меню ───────────────────────────── */

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Бот')
    .addItem('1. Ввести токен бота', 'menuSetToken')
    .addItem('2. Включить бота', 'setup')
    .addSeparator()
    .addItem('Тестовый режим: всё присылать мне', 'menuSetTestChat')
    .addItem('Выключить тестовый режим', 'menuClearTestChat')
    .addSeparator()
    .addItem('Предпросмотр рассылки (ничего не отправляет)', 'menuPreview')
    .addItem('Проверить, кого бот не узнаёт', 'menuCheckRoster')
    .addItem('Разослать дайджест сейчас', 'menuSendNow')
    .addSeparator()
    .addItem('Подключить сервер (aiogram)', 'menuBridgeOn')   // Bridge.gs
    .addItem('Отключить сервер', 'menuBridgeOff')
    .addSeparator()
    .addItem('Выключить бота', 'teardown')
    .addToUi();
}

function menuSetToken() {
  const ui = SpreadsheetApp.getUi();
  const res = ui.prompt('Токен бота',
    'Вставьте токен от @BotFather (вида 123456789:AA…). Он хранится в свойствах скрипта, в таблице его не видно.',
    ui.ButtonSet.OK_CANCEL);
  if (res.getSelectedButton() !== ui.Button.OK) return;
  const token = res.getResponseText().trim();
  if (!/^\d+:[\w-]{30,}$/.test(token)) { ui.alert('Это не похоже на токен бота. Скопируйте его из @BotFather целиком.'); return; }
  const props = PropertiesService.getScriptProperties();
  props.setProperty('TELEGRAM_TOKEN', token);
  props.setProperty('SPREADSHEET_ID', SpreadsheetApp.getActiveSpreadsheet().getId());
  const me = tgCall_('getMe', {});
  ui.alert('Подключено: @' + me.username + '\n\nДальше: «Бот → Тестовый режим», затем «Бот → 2. Включить бота».');
}

function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const props = PropertiesService.getScriptProperties();
  props.setProperty('SPREADSHEET_ID', ss.getId());
  const bridged = props.getProperty('BRIDGE_MODE');   // входящие читает сервер на aiogram (Bridge.gs)
  const me = tgCall_('getMe', {});
  if (!bridged) {
    tgCall_('deleteWebhook', {});   // бот читает сообщения через getUpdates
    tgCall_('setMyCommands', { commands: CLIENT_COMMANDS });
  }
  ScriptApp.getProjectTriggers().forEach(t => {
    if (['dailyJob', 'pollUpdates', 'onStatusEdit'].indexOf(t.getHandlerFunction()) >= 0) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('dailyJob').timeBased().everyDays(1).atHour(CONFIG.DAILY_HOUR)
    .inTimezone(ss.getSpreadsheetTimeZone()).create();
  if (!bridged) ScriptApp.newTrigger('pollUpdates').timeBased().everyMinutes(CONFIG.POLL_MINUTES).create();
  ScriptApp.newTrigger('onStatusEdit').forSpreadsheet(ss).onEdit().create();
  ensureLogSheet_();
  const test = props.getProperty('TEST_CHAT_ID');
  SpreadsheetApp.getUi().alert('Бот @' + me.username + ' включён.\n\n' +
    '• каждый день в ' + CONFIG.DAILY_HOUR + ':00 — напоминания, по понедельникам — дайджест;\n' +
    '• смена статуса на «Готово», «Риск», «Отменено» — сразу;\n' +
    (bridged ? '• входящие читает сервер (aiogram) и пересылает сюда команды сотрудников.\n\n' :
      '• входящие (/start) бот читает раз в ' + CONFIG.POLL_MINUTES + ' минут.\n\n') +
    (test ? 'Сейчас включён ТЕСТОВЫЙ режим: всё приходит только вам.' :
      'Тестовый режим выключен: сообщения уходят сотрудникам, которые написали боту /start.'));
}

function teardown() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  SpreadsheetApp.getUi().alert('Бот выключен: расписание и реакция на правки остановлены. Включить снова — «Бот → 2. Включить бота».');
}

function menuSetTestChat() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();
  const me = tgCall_('getMe', {});
  // Бот публичный: без кода тестовым чатом мог бы стать любой, кто в эти минуты напишет /test.
  // Пока код действует, повторное открытие окна показывает тот же код, а не обесценивает уже отправленный.
  let code = props.getProperty('TEST_CODE');
  if (!code || Date.now() >= Number(props.getProperty('TEST_ARM_UNTIL') || 0)) {
    code = String(100000 + Math.floor(Math.random() * 900000));
    props.setProperty('TEST_CODE', code);
    props.deleteProperty('TEST_TRIES');
  }
  props.setProperty('TEST_ARM_UNTIL', String(Date.now() + CONFIG.TEST_CODE_MINUTES * 60 * 1000));
  ui.alert('Тестовый режим', 'Откройте @' + me.username + ' в Telegram и отправьте ему:\n\n/test ' + code +
    '\n\nОтправили? Нажмите ОК — бот прочитает сообщение и ответит.', ui.ButtonSet.OK);
  // Сообщение может прийти с задержкой: проверяем несколько раз.
  for (let i = 0; i < 4 && props.getProperty('TEST_OK') !== code; i++) {
    if (i) Utilities.sleep(3000);
    pollUpdates();
  }
  ui.alert(props.getProperty('TEST_OK') === code
    ? 'Тестовый режим включён: все уведомления будут приходить только вам, с пометкой «для кого».'
    : 'Бот пока не получил /test ' + code + '. Отправьте его боту и снова откройте «Бот → Тестовый режим»: код останется тем же ещё ' +
      CONFIG.TEST_CODE_MINUTES + ' минут.');
}

function menuClearTestChat() {
  PropertiesService.getScriptProperties().deleteProperty('TEST_CHAT_ID');
  SpreadsheetApp.getUi().alert('Тестовый режим выключен. Сообщения пойдут сотрудникам, которые написали боту /start.');
}

function menuPreview() {
  const ctx = loadContext_();
  const today = todayStr_();
  const items = buildDigest_(ctx, today).concat(buildReminders_(ctx, today));
  const rows = items.map(it => [it.kind, whoItem_(it), it.person.chatId ? 'есть' : 'нет', plain_(it.text)]);
  writeSheet_(CONFIG.PREVIEW_SHEET,
    ['Предпросмотр на ' + dm_(today) + ': понедельничный дайджест (как если бы сегодня был понедельник) и напоминания на сегодня. Ничего не отправлено.'],
    ['Тип', 'Кому', 'chat_id', 'Текст сообщения'], rows, [110, 260, 70, 700]);
  SpreadsheetApp.getUi().alert('Готово: ' + rows.length + ' сообщений на листе «' + CONFIG.PREVIEW_SHEET + '». Ничего не отправлено.');
}

function menuCheckRoster() {
  const ctx = loadContext_();
  const count = new Map(), unknown = {};
  const note = (txt, id) => { (unknown[txt] = unknown[txt] || []).push(id); };
  ctx.tasks.forEach(t => {
    const r = matchPeople_(t.r, t.product, ctx.matcher);
    if (r.rest) note('R: ' + r.rest, t.id);
    if (!r.people.length) note('R не найден: ' + t.r, t.id);
    const aText = t.a || ctx.defaultA[t.dept] || '';
    const a = matchPeople_(aText, t.product, ctx.matcher);
    if (!a.people.length) note('A не найден: ' + (aText || '(пусто — нет утверждающего по умолчанию для отдела «' + t.dept + '»)'), t.id);
    r.people.concat(a.people).forEach(p => count.set(p, (count.get(p) || 0) + 1));
  });
  const rows = ctx.people.map(p => [p.role, p.name, count.get(p) || 0, p.username ? '@' + p.username : '—',
    p.chatId ? 'подписан' : 'не подписан', p.notify ? 'да' : 'нет']);
  rows.push(['', '', '', '', '', '']);
  rows.push(['Кого бот не узнал в задачах', 'Задач', 'Примеры ID', '', '', '']);
  Object.keys(unknown).sort((x, y) => unknown[y].length - unknown[x].length)
    .forEach(k => rows.push([k, unknown[k].length, unknown[k].slice(0, 6).join(', '), '', '', '']));
  writeSheet_(CONFIG.PREVIEW_SHEET, ['Проверка: кому бот будет писать и кого не узнаёт. Добавьте недостающие названия в столбец «Другие названия в задачах» листа «Команда».'],
    ['Роль', 'Имя', 'Задач (R или A)', 'Telegram', 'Подписка', 'Уведомлять'], rows, [300, 120, 110, 140, 110, 100]);
  SpreadsheetApp.getUi().alert('Готово, результат на листе «' + CONFIG.PREVIEW_SHEET + '».\nНе узнано названий: ' + Object.keys(unknown).length + '.');
}

function menuSendNow() {
  const ui = SpreadsheetApp.getUi();
  const test = PropertiesService.getScriptProperties().getProperty('TEST_CHAT_ID');
  const ok = ui.alert('Разослать дайджест сейчас?',
    test ? 'Тестовый режим: все сообщения придут только вам.' : 'Сообщения получат все подписанные сотрудники.',
    ui.ButtonSet.OK_CANCEL);
  if (ok !== ui.Button.OK) return;
  const log = sendOutbox_(buildDigest_(loadContext_(), todayStr_()));
  const sent = log.filter(r => String(r[5]).indexOf('отправлено') === 0).length;
  ui.alert('Отправлено: ' + sent + ' из ' + log.length + '. Подробности — на листе «' + CONFIG.LOG_SHEET + '».');
}

/* ─────────────────────────── Триггеры ─────────────────────────── */

/** Ежедневно в DAILY_HOUR: по понедельникам дайджест, каждый день — напоминания о сроках. */
function dailyJob() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const today = todayStr_();
    if (props.getProperty('LAST_DAILY') === today) return;
    const ctx = loadContext_();
    let items = buildReminders_(ctx, today);
    if (weekday_(today) === CONFIG.DIGEST_WEEKDAY) items = buildDigest_(ctx, today).concat(items);
    sendOutbox_(items);
    props.setProperty('LAST_DAILY', today);
  } finally {
    lock.releaseLock();
  }
}

/** Устанавливаемый триггер «При изменении»: реагирует на смену статуса. */
function onStatusEdit(e) {
  try {
    if (!e || !e.range) return;
    const sh = e.range.getSheet();
    if (e.range.getLastRow() < 2) return;
    const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    if (!isTaskSheet_(sh, head)) return;
    const stCol = head.map(h => String(h).trim()).indexOf(COL.status) + 1;
    if (!stCol || stCol < e.range.getColumn() || stCol > e.range.getLastColumn()) return;
    const ctx = loadContext_();
    const gid = sh.getSheetId(), r1 = Math.max(2, e.range.getRow()), r2 = e.range.getLastRow();
    const edited = ctx.tasks.filter(t => t.gid === gid && t.row >= r1 && t.row <= r2);
    const items = buildStatusEvents_(ctx, edited, PropertiesService.getScriptProperties());
    if (items.length) sendOutbox_(items);
    if (items.skipped.length) {
      appendLog_(items.skipped.map(s => [new Date(), s.kind, s.who, '', s.id, 'не отправлено: ' + s.reason, '']));
    }
  } catch (err) {
    logEvent_('ошибка', 'onStatusEdit', '', String((err && err.message) || err));
  }
}

/** Раз в POLL_MINUTES: читает входящие сообщения боту. */
function pollUpdates() {
  if (PropertiesService.getScriptProperties().getProperty('BRIDGE_MODE')) return;   // входящие читает сервер (Bridge.gs)
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    let offset = Number(props.getProperty('TG_OFFSET') || 0);
    const updates = tgCall_('getUpdates', { offset: offset, timeout: 0, allowed_updates: ['message'] });
    if (!updates.length) return;
    let team = null;
    updates.forEach(u => {
      offset = u.update_id + 1;
      try {
        if (u.message) { team = team || loadTeam_(ss_()); handleMessage_(u.message, team); }
      } catch (err) {
        logEvent_('ошибка', 'входящее сообщение', '', String((err && err.message) || err));
      }
    });
    props.setProperty('TG_OFFSET', String(offset));
  } finally {
    lock.releaseLock();
  }
}

/* ──────────────────────── Входящие команды ─────────────────────── */

function handleMessage_(m, team) {
  const text = String(m.text || '').trim();
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase().replace(/@.*$/, '');
  const arg = parts.slice(1).join(' ');
  if (m.chat.type !== 'private') {
    if (cmd === '/start') registerGroup_(m, team, arg);
    return;   // в общих чатах бот молчит, кроме подписки чата
  }
  const staff = findStaff_(m, team);
  if (cmd === '/test') return armTest_(m, staff.length > 0, arg);
  if (cmd === '/team') return registerStaff_(m, team);
  if (!staff.length) return clientReply_(m, cmd === '/start');
  if (cmd === '/start') return registerStaff_(m, team);
  if (cmd === '/my') return sendMy_(m);
  reply_(m.chat.id, STAFF_HELP);
}

/** Сотрудник — тот, чей chat_id или @username есть на листе «Команда». */
function findStaff_(m, team) {
  const chatId = String(m.chat.id);
  const u = String((m.from && m.from.username) || '').toLowerCase();
  return team.people.filter(p => p.chatId === chatId || (u && p.username === u));
}

function clientReply_(m, isStart) {
  if (typeof BRIDGED_ !== 'undefined' && BRIDGED_) return 'client';   // клиентов обслуживает сервер (Bridge.gs)
  if (isStart) {
    const u = (m.from && m.from.username) ? '@' + m.from.username : String((m.from && m.from.first_name) || '');
    logEvent_('клиент', u, m.chat.id, 'нажал /start');
  }
  reply_(m.chat.id, CONFIG.CLIENT_GREETING + (CONFIG.CLIENT_CONTACT ? '\n\n' + CONFIG.CLIENT_CONTACT : ''));
}

/** Общий чат (например, продавцов): /start Продавцы. Можно только ролям с «группа» в столбце username. */
function registerGroup_(m, team, arg) {
  const chatId = String(m.chat.id);
  if (!arg) return reply_(chatId, 'Чтобы подписать этот чат на роль, отправьте: /start Название роли (как на листе «Команда»), например /start Продавцы');
  const matched = team.people.filter(p => norm_(p.role) === norm_(arg) && p.username === 'группа');
  if (!matched.length) return reply_(chatId, 'Не могу подписать чат на «' + arg + '». На листе «Команда» у этой роли в столбце «Telegram @username» должно стоять слово «группа».');
  saveChat_(team, matched, chatId);
  reply_(chatId, 'Готово, этот чат подписан: ' + matched.map(p => p.role).join(', ') + '.');
}

/** Подписка сотрудника по @username. Только для тех, кто есть на листе «Команда» (или явно прислал /team). */
function registerStaff_(m, team) {
  const chatId = String(m.chat.id);
  const u = String((m.from && m.from.username) || '').toLowerCase();
  const matched = team.people.filter(p => (u && p.username === u) || p.chatId === chatId);
  if (!matched.length) {
    logEvent_('регистрация', u ? '@' + u : '(без username)', chatId, 'нет на листе «Команда»');
    if (!u) return reply_(chatId, 'В вашем Telegram не задано имя пользователя (Настройки → Имя пользователя). Задайте его и снова отправьте /team.\n\nИли передайте руководителю ваш chat_id: ' + chatId);
    return reply_(chatId, 'Не нашёл @' + u + ' в списке команды. Попросите руководителя вписать ваш username напротив вашей роли и снова отправьте /team.');
  }
  saveChat_(team, matched, chatId);
  try { tgCall_('setMyCommands', { commands: STAFF_COMMANDS, scope: { type: 'chat', chat_id: chatId } }); } catch (err) { /* меню — не главное */ }
  const roles = matched.map(p => p.role).join(', ');
  reply_(chatId, 'Готово, вы подписаны: ' + roles + '.\n\n' +
    'Что я буду присылать:\n' +
    '• по понедельникам в ' + CONFIG.DAILY_HOUR + ':00 — задачи на неделю и просроченные;\n' +
    '• за ' + CONFIG.REMIND_DAYS_BEFORE + ' ' + plural_(CONFIG.REMIND_DAYS_BEFORE, 'день', 'дня', 'дней') + ' до срока — напоминание о незакрытых задачах;\n' +
    '• если вы утверждаете задачу — сообщение, когда её сдали или она попала в «Риск».\n\n' +
    '/my — мои задачи прямо сейчас');
}

function saveChat_(team, people, chatId) {
  people.forEach(p => {
    team.sheet.getRange(p.row, team.idx.chatId + 1).setValue(chatId);
    p.chatId = chatId;
  });
  logEvent_('регистрация', people.map(p => p.role).join(', '), chatId, 'подписан');
}

function sendMy_(m) {
  const chatId = String(m.chat.id);
  const ctx = loadContext_();
  const mine = ctx.people.filter(p => p.chatId === chatId);
  if (!mine.length) return reply_(chatId, 'Вы ещё не подписаны. Отправьте /team.');
  const keys = new Set(mine.map(personKey_));
  const items = buildDigest_(ctx, todayStr_(), keys);
  if (!items.length) return reply_(chatId, 'На этой неделе задач по плану у вас нет, просроченных тоже нет 👍');
  items.forEach(it => sendLong_(chatId, it.text));
}

function armTest_(m, isStaff, code) {
  const props = PropertiesService.getScriptProperties();
  const expected = props.getProperty('TEST_CODE');
  const disarm = () => ['TEST_ARM_UNTIL', 'TEST_CODE', 'TEST_TRIES'].forEach(k => props.deleteProperty(k));
  if (!expected || Date.now() >= Number(props.getProperty('TEST_ARM_UNTIL') || 0)) {
    if (!isStaff) return clientReply_(m, false);
    return reply_(m.chat.id, 'Команда /test работает только сразу после «Бот → Тестовый режим» в таблице, с кодом из окна: /test 123456.');
  }
  if (code === expected) {
    props.setProperty('TEST_CHAT_ID', String(m.chat.id));
    props.setProperty('TEST_OK', expected);
    disarm();
    return reply_(m.chat.id, 'Тестовый режим включён: все уведомления будут приходить сюда, с пометкой «для кого».');
  }
  if (!code) return reply_(m.chat.id, 'Добавьте код из окна в таблице, например: /test 123456');
  const tries = Number(props.getProperty('TEST_TRIES') || 0) + 1;
  if (tries >= CONFIG.TEST_MAX_TRIES) {
    disarm();
    return reply_(m.chat.id, 'Слишком много неверных кодов. Откройте «Бот → Тестовый режим» в таблице ещё раз.');
  }
  props.setProperty('TEST_TRIES', String(tries));
  reply_(m.chat.id, 'Код не подошёл. Отправьте код из последнего окна в таблице: /test 123456');
}

/* ─────────────────────── Сборка сообщений ─────────────────────── */

function isOpen_(t) { return t.status !== STATUS.DONE && t.status !== STATUS.CANCELLED; }

function sortTasks_(list) {
  return list.slice().sort((x, y) => (x.due < y.due ? -1 : x.due > y.due ? 1 : ((x.gid || 0) - (y.gid || 0)) || x.row - y.row));
}

/** Недельный дайджест. onlyKeys — если задан, только для этих людей (команда /my). */
function buildDigest_(ctx, today, onlyKeys) {
  const wStart = addDays_(today, 1 - weekday_(today));
  const wEnd = addDays_(wStart, 6);
  const groups = new Map();
  const add = (p, list, t) => {
    const k = personKey_(p);
    if (onlyKeys && !onlyKeys.has(k)) return;
    if (!groups.has(k)) groups.set(k, { people: [], rWeek: [], rOver: [], aWeek: [], aOver: [], aRisk: [] });
    const g = groups.get(k);
    if (g.people.indexOf(p) < 0) g.people.push(p);
    if (g[list].indexOf(t) < 0) g[list].push(t);
  };
  let weekLabel = '';
  ctx.tasks.forEach(t => {
    if (!t.due) return;
    const inWeek = t.due >= wStart && t.due <= wEnd;
    const over = t.due < today && isOpen_(t) && !inWeek;
    const risk = t.status === STATUS.RISK;
    if (inWeek && !weekLabel) weekLabel = t.week;
    if (!inWeek && !over && !risk) return;
    const rs = ctx.rOf(t), as = ctx.aOf(t);
    const rKeys = new Set(rs.map(personKey_));
    rs.forEach(p => { if (inWeek) add(p, 'rWeek', t); if (over) add(p, 'rOver', t); });
    as.forEach(p => {
      if (rKeys.has(personKey_(p))) return;   // свои задачи человек видит в разделе исполнителя
      if (inWeek) add(p, 'aWeek', t);
      if (over) add(p, 'aOver', t);
      if (risk) add(p, 'aRisk', t);
    });
  });
  weekLabel = weekLabel || ('неделя ' + dm_(wStart) + '–' + dm_(wEnd));
  const items = [];
  groups.forEach(g => {
    const text = digestText_(ctx, g, weekLabel, wEnd);
    if (!text) return;
    const ids = [].concat(g.rWeek, g.rOver, g.aOver, g.aRisk).map(t => t.id);
    items.push({ kind: 'дайджест', person: g.people[0], people: g.people, text: text, ids: ids });
  });
  return items;
}

function digestText_(ctx, g, weekLabel, wEnd) {
  const lines = [];
  const rOpen = sortTasks_(g.rWeek.filter(isOpen_));
  if (g.rWeek.length || g.rOver.length) {
    lines.push('📋 <b>Ваши задачи · ' + esc_(weekLabel) + '</b>');
    if (rOpen.length) {
      lines.push('Срок — ' + dueLabel_(wEnd) + '.', '');
      lines.push.apply(lines, listLines_(ctx, rOpen, { result: true, status: true }, true));
    } else if (g.rWeek.length) {
      lines.push('Задачи этой недели уже закрыты 👍');
    } else {
      lines.push('На этой неделе новых задач по плану нет.');
    }
    if (g.rOver.length) {
      lines.push('', '❗ <b>Просрочено</b> (' + g.rOver.length + '):');
      lines.push.apply(lines, listLines_(ctx, sortTasks_(g.rOver), { due: true, status: true, max: CONFIG.OVERDUE_MAX }));
    }
  }
  if (g.aWeek.length || g.aOver.length || g.aRisk.length) {
    if (lines.length) lines.push('');
    const done = g.aWeek.filter(t => t.status === STATUS.DONE).length;
    lines.push('📊 <b>Вы утверждаете · ' + esc_(weekLabel) + '</b>');
    lines.push('На этой неделе ' + g.aWeek.length + ' ' + plural_(g.aWeek.length, 'задача', 'задачи', 'задач') + ', готово ' + done + '.');
    if (g.aOver.length) {
      // Руководителю — у кого сколько просрочено и самые давние задачи, без длинного списка.
      const byWho = {};
      g.aOver.forEach(t => { byWho[t.r] = (byWho[t.r] || 0) + 1; });
      const who = Object.keys(byWho).sort((x, y) => byWho[y] - byWho[x]);
      lines.push('', '❗ <b>Просрочено у исполнителей: ' + g.aOver.length + '</b>');
      lines.push(who.slice(0, 8).map(w => esc_(w) + ' — ' + byWho[w]).join('; ') + (who.length > 8 ? '; …' : ''));
      lines.push('Самые давние:');
      lines.push.apply(lines, listLines_(ctx, sortTasks_(g.aOver), { due: true, who: true, max: CONFIG.APPROVER_TOP }));
    }
    if (g.aRisk.length) {
      lines.push('', '⚠️ <b>В статусе «Риск»</b> (' + g.aRisk.length + '):');
      lines.push.apply(lines, listLines_(ctx, sortTasks_(g.aRisk), { due: true, who: true, comment: true }));
    }
  }
  if (!lines.length) return '';
  lines.push('', footer_(ctx));
  return lines.join('\n');
}

/** Напоминание исполнителям за REMIND_DAYS_BEFORE дней до срока. */
function buildReminders_(ctx, today) {
  const target = addDays_(today, CONFIG.REMIND_DAYS_BEFORE);
  const groups = new Map();
  ctx.tasks.forEach(t => {
    if (t.due !== target || !isOpen_(t)) return;
    ctx.rOf(t).forEach(p => {
      const k = personKey_(p);
      if (!groups.has(k)) groups.set(k, { people: [], tasks: [] });
      const g = groups.get(k);
      if (g.people.indexOf(p) < 0) g.people.push(p);
      if (g.tasks.indexOf(t) < 0) g.tasks.push(t);
    });
  });
  const n = CONFIG.REMIND_DAYS_BEFORE;
  const items = [];
  groups.forEach(g => {
    const lines = ['⏰ <b>До срока ' + n + ' ' + plural_(n, 'день', 'дня', 'дней') + '</b> (' + dueLabel_(target) + '). Ещё не закрыты:', ''];
    lines.push.apply(lines, listLines_(ctx, sortTasks_(g.tasks), { status: true, result: true }));
    lines.push('', footer_(ctx));
    items.push({ kind: 'напоминание', person: g.people[0], people: g.people, text: lines.join('\n'), ids: g.tasks.map(t => t.id) });
  });
  return items;
}

/** Сообщения о смене статуса. props — где помнить последний отправленный статус задачи. */
function buildStatusEvents_(ctx, tasks, props) {
  const items = [];
  items.skipped = [];   // смена статуса, о которой некому сообщить, — с причиной для «Журнала бота»
  tasks.forEach(t => {
    const key = 'ST_' + t.id;
    if (props) {
      if (props.getProperty(key) === t.status) return;
      props.setProperty(key, t.status);
    }
    const rs = ctx.rOf(t);
    const rKeys = new Set(rs.map(personKey_));
    const approvers = ctx.aOf(t).filter(p => !rKeys.has(personKey_(p)));
    let targets = [], kind = '', text = '';
    const head = (icon, title) => icon + ' <b>' + title + '</b> · ' + link_(ctx, t) + '\n' + esc_(cut_(t.task, 300));
    if (t.status === STATUS.DONE) {
      targets = approvers; kind = 'на приёмку';
      const late = t.fact && t.fact > t.due ? ' (на ' + daysBetween_(t.due, t.fact) + ' дн. позже срока)' : '';
      text = head('✅', 'Сдано на приёмку') + '\n\nИсполнитель: ' + esc_(t.r) + ' · срок ' + dueLabel_(t.due) +
        (t.fact ? ' · сдано ' + dm_(t.fact) + late : '') +
        (t.result ? '\nКритерий готовности: ' + esc_(t.result) : '') +
        '\n\nЕсли результат не принят — верните статус «В работе» и напишите в комментарии, что доделать.';
    } else if (t.status === STATUS.RISK) {
      targets = approvers; kind = 'риск';
      text = head('⚠️', 'Риск по задаче') + '\n\nИсполнитель: ' + esc_(t.r) + ' · срок ' + dueLabel_(t.due) +
        '\n' + (t.comment ? 'Комментарий: ' + esc_(t.comment) : 'Комментарий пока не заполнен — уточните причину у исполнителя.');
    } else if (t.status === STATUS.CANCELLED) {
      targets = rs; kind = 'снята';
      text = head('🚫', 'Задача снята') + '\n\n' + (t.comment ? 'Причина: ' + esc_(t.comment) : 'Причина не указана.');
    }
    if (kind && !targets.length) {
      const allA = ctx.aOf(t);
      const reason = kind === 'снята' ? 'исполнитель «' + t.r + '» не найден на листе «Команда»'
        : !allA.length ? 'утверждающий не найден на листе «Команда»'
          : 'утверждающий сам среди исполнителей — сообщать некому';
      items.skipped.push({ kind: kind, id: t.id, who: (kind === 'снята' ? t.r : allA.map(p => p.role).join(', ') || '—'), reason: reason });
    }
    const seen = new Set();
    targets.forEach(p => {
      const k = personKey_(p);
      if (seen.has(k)) return;
      seen.add(k);
      items.push({ kind: kind, person: p, people: [p], text: text, ids: [t.id] });
    });
  });
  return items;
}

function listLines_(ctx, tasks, opts, numbered) {
  const lines = [];
  const max = opts.max || CONFIG.MAX_ITEMS;
  tasks.slice(0, max).forEach((t, k) => {
    const meta = [];
    if (opts.due) meta.push('срок ' + dueLabel_(t.due));
    if (opts.who) meta.push(esc_(t.r));
    if (opts.status && t.status !== STATUS.NEW) meta.push(esc_(t.status));
    lines.push((numbered ? (k + 1) + '. ' : '• ') + link_(ctx, t) + ' · ' + esc_(cut_(t.task, CONFIG.TASK_TEXT_MAX)) +
      (meta.length ? ' <i>(' + meta.join(', ') + ')</i>' : ''));
    if (opts.result && t.result) lines.push('     ✔ <i>Готово, когда:</i> ' + esc_(cut_(t.result, CONFIG.RESULT_TEXT_MAX)));
    if (opts.comment && t.comment) lines.push('     💬 ' + esc_(cut_(t.comment, 200)));
  });
  if (tasks.length > max) lines.push('…и ещё ' + (tasks.length - max) + ' — в таблице');
  return lines;
}

function footer_(ctx) {
  return 'Статус меняйте в <a href="' + esc_(ctx.url) + '">таблице</a>: «В работе» → «Готово» и дата факт; если срок под угрозой — «Риск» с комментарием.';
}

function link_(ctx, t) {
  const gid = t.gid != null ? t.gid : ctx.gid;
  return '<a href="' + esc_(ctx.url + '#gid=' + gid + '&range=A' + t.row) + '">' + esc_(t.id) + '</a>';
}

/* ─────────────────── Сопоставление ролей и людей ────────────────── */

function norm_(s) {
  return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
}

function aliasRegex_(alias, flags) {
  const esc = alias.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('(^|[^\\p{L}\\p{N}])' + esc + '(?=$|[^\\p{L}\\p{N}])', 'u' + (flags || ''));
}

/** Все названия ролей, от длинных к коротким: «Монтажёр №2» проверяется раньше «Монтажёр». */
function buildMatcher_(people) {
  const map = new Map();
  const add = (alias, p, generic) => {
    alias = norm_(alias);
    if (!alias) return;
    const key = (generic ? 'g:' : 's:') + alias;
    if (!map.has(key)) map.set(key, { alias: alias, generic: generic, people: [] });
    const e = map.get(key);
    if (e.people.indexOf(p) < 0) e.people.push(p);
  };
  people.forEach(p => {
    add(p.role, p, false);
    add(p.name, p, false);
    p.aliases.forEach(a => add(a, p, false));
    p.generic.forEach(a => add(a, p, true));
  });
  const list = Array.from(map.values());
  list.forEach(e => { e.re = aliasRegex_(e.alias); e.reAll = aliasRegex_(e.alias, 'g'); });
  return list.sort((x, y) => (y.alias.length - x.alias.length) || (x.generic - y.generic));
}

/**
 * Находит людей по тексту ячейки «Исполнитель»/«Утверждает».
 * Общее название («методист», «суппорт-тичер») уточняется по продукту задачи.
 * rest — то, что не удалось распознать (для проверки на листе предпросмотра).
 */
function matchPeople_(text, product, matcher) {
  let t = ' ' + norm_(text) + ' ';
  const found = [];
  const byKey = {};
  for (let i = 0; i < matcher.length; i++) {
    const e = matcher[i];
    if (!e.re.test(t)) continue;
    let cands = e.people;
    if (e.generic) {
      const prod = norm_(product);
      const f = cands.filter(p => p.products.some(x => norm_(x) === prod));
      if (f.length) cands = f;
    }
    cands.forEach(p => { if (found.indexOf(p) < 0) found.push(p); });
    byKey[e.alias] = true;
    // Одинаковое название может быть у нескольких записей — «съедаем» его после всех.
    const next = matcher[i + 1];
    if (!next || next.alias !== e.alias) t = t.replace(e.reAll, '$1 ');
  }
  let rest = t.replace(/\([^)]*\)/g, ' ').replace(/[+\/,;:]/g, ' ');
  CONFIG.IGNORE_WORDS.concat(['и']).forEach(w => { rest = rest.replace(aliasRegex_(norm_(w), 'g'), '$1 '); });
  rest = rest.replace(/\s+/g, ' ').trim();
  return { people: found, rest: rest };
}

function personKey_(p) {
  if (p.chatId) return 'c' + p.chatId;
  if (p.username && p.username !== 'группа') return 'u' + p.username;
  return 'r' + p.row;
}

/* ─────────────────────── Чтение таблицы ─────────────────────── */

function ss_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  return id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
}

const TASK_SHEET_COLS = ['id', 'due', 'task', 'r', 'status'];

/** Лист с задачами отдела: в первой строке есть нужные заголовки, и это не общий собранный лист. */
function isTaskSheet_(sh, headRow) {
  if (sh.getName() === CONFIG.ALL_TASKS_SHEET) return false;
  const head = headRow.map(h => String(h).trim());
  return TASK_SHEET_COLS.every(k => head.indexOf(COL[k]) >= 0);
}

function loadContext_() {
  const ss = ss_();
  const tz = ss.getSpreadsheetTimeZone();
  const tasks = [];
  let sheets = 0;
  ss.getSheets().forEach(sh => {
    if (sh.getLastRow() < 1 || sh.getLastColumn() < 1) return;
    const values = sh.getDataRange().getValues();
    if (!isTaskSheet_(sh, values[0])) return;
    sheets++;
    const idx = headerIndex_(values[0], COL, TASK_SHEET_COLS);
    const where = { name: sh.getName(), gid: sh.getSheetId() };
    for (let i = 1; i < values.length; i++) {
      const t = rowToTask_(values[i], idx, i + 1, tz, where);
      if (t.id) tasks.push(t);
    }
  });
  if (!sheets) throw new Error('Не нашёл листов с задачами: нужны столбцы «ID», «Статус», «Исполнитель (R)», «Срок», «Задача»');
  const team = loadTeam_(ss);
  return makeContext_({
    tasks: tasks, people: team.people, defaultA: loadDefaultApprovers_(ss, tasks),
    url: ss.getUrl(), gid: 0,
  });
}

function makeContext_(base) {
  const ctx = Object.assign({}, base);
  ctx.matcher = buildMatcher_(ctx.people);
  const cache = {};
  const key = t => (t.gid != null ? t.gid + ':' : '') + t.row;   // строки разных листов не путаются
  ctx.rOf = t => cache['r' + key(t)] || (cache['r' + key(t)] = matchPeople_(t.r, t.product, ctx.matcher).people);
  ctx.aOf = t => cache['a' + key(t)] ||
    (cache['a' + key(t)] = matchPeople_(t.a || ctx.defaultA[t.dept] || '', t.product, ctx.matcher).people);
  return ctx;
}

function headerIndex_(headRow, spec, required) {
  const head = headRow.map(h => String(h).trim());
  const idx = {};
  Object.keys(spec).forEach(k => { idx[k] = head.indexOf(spec[k]); });
  (required || []).forEach(k => { if (idx[k] < 0) throw new Error('Не найден столбец «' + spec[k] + '»'); });
  return idx;
}

function rowToTask_(row, idx, rowNum, tz, where) {
  const g = k => (idx[k] >= 0 ? row[idx[k]] : '');
  const s = k => String(g(k) == null ? '' : g(k)).trim();
  const due = g('due'), fact = g('fact'), start = g('start');
  return {
    sheet: where ? where.name : '', gid: where ? where.gid : undefined,
    start: start instanceof Date ? dstr_(start, tz) : '',
    row: rowNum, id: s('id'), week: s('week'), dept: s('dept'), product: s('product'), task: s('task'),
    r: s('r'), status: s('status') || STATUS.NEW, comment: s('comment'), result: s('result'),
    a: s('a'), c: s('c'), i: s('i'),
    due: due instanceof Date ? dstr_(due, tz) : '',
    fact: fact instanceof Date ? dstr_(fact, tz) : '',
  };
}

function loadTeam_(ss) {
  const sh = ss.getSheetByName(CONFIG.TEAM_SHEET);
  if (!sh) throw new Error('Нет листа «' + CONFIG.TEAM_SHEET + '»');
  const values = sh.getDataRange().getValues();
  const idx = headerIndex_(values[0], TEAM, ['role', 'username', 'chatId']);
  const people = [];
  for (let i = 1; i < values.length; i++) {
    const row = values[i];
    const g = k => (idx[k] >= 0 ? String(row[idx[k]] == null ? '' : row[idx[k]]).trim() : '');
    if (!g('role')) continue;
    people.push({
      row: i + 1, role: g('role'), name: g('name'), dept: g('dept'),
      aliases: splitList_(g('aliases')), generic: splitList_(g('generic')), products: splitList_(g('products')),
      username: g('username').replace(/^@/, '').toLowerCase(), chatId: g('chatId'),
      notify: norm_(g('notify')) !== 'нет',
    });
  }
  return { sheet: sh, idx: idx, people: people };
}

/** Утверждающий по умолчанию для отдела — таблица «Отдел → Утверждает по умолчанию (A)» на листе «Справочник». */
function loadDefaultApprovers_(ss, tasks) {
  const depts = new Set(tasks.map(t => t.dept));
  const map = {};
  const sh = ss.getSheetByName(CONFIG.REF_SHEET);
  if (!sh) return map;
  sh.getDataRange().getValues().forEach(r => {
    const d = String(r[1] || '').trim(), a = String(r[2] || '').trim();
    if (depts.has(d) && a && !map[d]) map[d] = a;
  });
  return map;
}

function splitList_(s) {
  return String(s || '').split(/[;\n]/).map(x => x.trim()).filter(Boolean);
}

/* ─────────────────────────── Отправка ─────────────────────────── */

function sendOutbox_(items) {
  const testChat = PropertiesService.getScriptProperties().getProperty('TEST_CHAT_ID');
  const log = [];
  items.forEach(it => {
    const p = it.person;
    const chat = testChat || p.chatId;
    let text = it.text, res;
    if (testChat) text = '🧪 <i>Тест · для: ' + esc_(whoItem_(it)) + '</i>\n\n' + text;
    if (!testChat && !p.notify) res = 'уведомления выключены';
    else if (!chat) res = 'нет chat_id — сотрудник ещё не написал боту /start';
    else {
      try { sendLong_(chat, text); res = testChat ? 'отправлено (тест)' : 'отправлено'; Utilities.sleep(60); }
      catch (err) { res = 'ошибка: ' + ((err && err.message) || err); }
    }
    log.push([new Date(), it.kind, whoItem_(it), String(chat || ''), (it.ids || []).join(', '), res, plain_(text).slice(0, 1500)]);
  });
  appendLog_(log);
  return log;
}

function sendLong_(chatId, text) {
  const chunks = [];
  let cur = '';
  text.split('\n').forEach(line => {
    if ((cur + '\n' + line).length > 3800 && cur) { chunks.push(cur); cur = line; }
    else cur = cur ? cur + '\n' + line : line;
  });
  if (cur) chunks.push(cur);
  chunks.forEach(c => tgCall_('sendMessage', { chat_id: chatId, text: c, parse_mode: 'HTML', disable_web_page_preview: true }));
}

function reply_(chatId, text) {
  tgCall_('sendMessage', { chat_id: chatId, text: text, disable_web_page_preview: true });
}

function tgCall_(method, payload) {
  const token = PropertiesService.getScriptProperties().getProperty('TELEGRAM_TOKEN');
  if (!token) throw new Error('Не задан токен бота: меню «Бот → 1. Ввести токен бота».');
  const resp = UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/' + method, {
    method: 'post', contentType: 'application/json', payload: JSON.stringify(payload), muteHttpExceptions: true,
  });
  let body = {};
  try { body = JSON.parse(resp.getContentText() || '{}'); } catch (err) { /* ответ не JSON */ }
  if (!body.ok) throw new Error('Telegram ' + method + ': ' + (body.description || ('HTTP ' + resp.getResponseCode())));
  return body.result;
}

/* ───────────────────────── Журнал и листы ───────────────────────── */

function ensureLogSheet_() {
  const ss = ss_();
  let sh = ss.getSheetByName(CONFIG.LOG_SHEET);
  if (!sh) {
    sh = ss.insertSheet(CONFIG.LOG_SHEET);
    sh.getRange(1, 1, 1, 7).setValues([['Время', 'Тип', 'Кому', 'chat_id', 'Задачи', 'Результат', 'Текст']])
      .setFontWeight('bold').setBackground('#1f3b66').setFontColor('#ffffff');
    sh.setFrozenRows(1);
    [140, 110, 240, 110, 160, 260, 600].forEach((w, i) => sh.setColumnWidth(i + 1, w));
  }
  return sh;
}

function appendLog_(rows) {
  if (!rows.length) return;
  const sh = ensureLogSheet_();
  sh.getRange(sh.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
}

function logEvent_(kind, who, chat, result) {
  try { appendLog_([[new Date(), kind, who, String(chat || ''), '', result, '']]); } catch (err) { console.error(err); }
}

function writeSheet_(name, notes, header, rows, widths) {
  const ss = ss_();
  const sh = ss.getSheetByName(name) || ss.insertSheet(name);
  sh.clear();
  notes.forEach((n, i) => sh.getRange(i + 1, 1).setValue(n).setFontStyle('italic'));
  const top = notes.length + 2;
  sh.getRange(top, 1, 1, header.length).setValues([header]).setFontWeight('bold')
    .setBackground('#1f3b66').setFontColor('#ffffff');
  if (rows.length) {
    sh.getRange(top + 1, 1, rows.length, header.length).setValues(rows.map(r => r.slice(0, header.length)))
      .setWrap(true).setVerticalAlignment('top');
  }
  (widths || []).forEach((w, i) => sh.setColumnWidth(i + 1, w));
  sh.activate();
}

/* ─────────────────────────── Мелочи ─────────────────────────── */

function whoItem_(it) {
  return (it.people || [it.person]).map(p => (p.name && norm_(p.name) !== norm_(p.role) ? p.role + ' · ' + p.name : p.role)).join(', ');
}

function todayStr_() { return dstr_(new Date(), ss_().getSpreadsheetTimeZone()); }
function dstr_(d, tz) { return Utilities.formatDate(d, tz, 'yyyy-MM-dd'); }
function utc_(s) { return Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)); }
function addDays_(s, n) { return new Date(utc_(s) + n * 864e5).toISOString().slice(0, 10); }
function daysBetween_(a, b) { return Math.round((utc_(b) - utc_(a)) / 864e5); }
function weekday_(s) { return new Date(utc_(s)).getUTCDay() || 7; }   // 1 = пн … 7 = вс
function dm_(s) { return s ? s.slice(8, 10) + '.' + s.slice(5, 7) : ''; }
function dueLabel_(s) { return s ? ['', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][weekday_(s)] + ' ' + dm_(s) : '—'; }

function plural_(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

function esc_(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function cut_(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1).trim() + '…' : s; }
function plain_(html) { return String(html).replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'); }
