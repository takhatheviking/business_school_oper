/**
 * Мост к серверу на aiogram (папка server/ в репозитории).
 *
 * Входящие сообщения бота может читать только кто-то один. Когда сервер подключён, их читает он:
 * клиентам отвечает сам (курсы, заявки, мероприятия, билеты), а команды сотрудников (/my, /team,
 * /test, подписку) пересылает сюда, в веб-приложение этого скрипта. Скрипт отвечает сотруднику
 * сам, тем же кодом, что и раньше. Рассылки (дайджест, напоминания, смена статуса) по-прежнему
 * идут отсюда.
 *
 * Как подключить — bot/README.md, раздел «Сервер на aiogram».
 */

let BRIDGED_ = false;   // true внутри doPost: clientReply_ не отвечает клиенту, а говорит серверу «это клиент»

/** Запросы сервера: {secret, action: 'staff'} или {secret, action: 'message', message: {...}}. */
function doPost(e) {
  let req;
  try {
    req = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return json_({ ok: false, error: 'bad json' });
  }
  const secret = PropertiesService.getScriptProperties().getProperty('BRIDGE_SECRET');
  if (!secret || req.secret !== secret) return json_({ ok: false, error: 'forbidden' });
  BRIDGED_ = true;
  try {
    const team = loadTeam_(ss_());
    if (req.action === 'staff') {
      const uniq = list => list.filter((x, i) => x && list.indexOf(x) === i);
      return json_({
        ok: true,
        usernames: uniq(team.people.map(p => p.username).filter(u => u !== 'группа')),
        chat_ids: uniq(team.people.map(p => p.chatId)),
      });
    }
    if (req.action === 'message' && req.message && req.message.chat) {
      const res = handleMessage_(req.message, team);
      return json_({ ok: true, client: res === 'client' });
    }
    return json_({ ok: false, error: 'unknown action' });
  } catch (err) {
    logEvent_('ошибка', 'мост', '', String((err && err.message) || err));
    return json_({ ok: false, error: 'script error' });
  } finally {
    BRIDGED_ = false;
  }
}

/** Открыть адрес веб-приложения в браузере — проверка, что развёртывание живое. */
function doGet() {
  return ContentService.createTextOutput('Мост бота работает. Сервер обращается сюда POST-запросами.');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Меню «Бот → Подключить сервер (aiogram)». */
function menuBridgeOn() {
  const ui = SpreadsheetApp.getUi();
  const ok = ui.alert('Подключить сервер?',
    'После этого таблица перестанет сама читать сообщения боту — их будет читать сервер.\n\n' +
    'Сначала опубликуйте этот скрипт как веб-приложение (bot/README.md, раздел «Сервер на aiogram»).',
    ui.ButtonSet.OK_CANCEL);
  if (ok !== ui.Button.OK) return;
  const props = PropertiesService.getScriptProperties();
  let secret = props.getProperty('BRIDGE_SECRET');
  if (!secret) {
    secret = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '');
    props.setProperty('BRIDGE_SECRET', secret);
  }
  props.setProperty('BRIDGE_MODE', '1');
  ScriptApp.getProjectTriggers().forEach(t => { if (t.getHandlerFunction() === 'pollUpdates') ScriptApp.deleteTrigger(t); });
  ui.alert('Сервер подключён',
    'Опрос входящих в таблице выключен. Впишите в файл .env на сервере:\n\n' +
    'BRIDGE_SECRET=' + secret + '\n' +
    'BRIDGE_URL=<адрес веб-приложения, …/exec>\n\n' +
    'и перезапустите сервер. Секрет никому не пересылайте: с ним можно выдать себя за бота.',
    ui.ButtonSet.OK);
}

/** Меню «Бот → Отключить сервер»: таблица снова читает входящие сама. */
function menuBridgeOff() {
  const ui = SpreadsheetApp.getUi();
  const props = PropertiesService.getScriptProperties();
  props.deleteProperty('BRIDGE_MODE');
  props.deleteProperty('BRIDGE_SECRET');   // старый секрет больше не подходит
  ui.alert('Сервер отключён',
    'Остановите сервер (docker compose down), затем нажмите «Бот → 2. Включить бота» — таблица снова будет читать входящие раз в ' +
    CONFIG.POLL_MINUTES + ' минут. Пока работают оба, сообщения могут теряться.',
    ui.ButtonSet.OK);
}
