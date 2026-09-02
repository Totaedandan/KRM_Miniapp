# Гейт подписки на канал — фикс для второго бота

Как у конкурентов (Rent Mao Bot / Zenly Store): без подписки на канал Mini App
не даёт доступ к функционалу — полноэкранный блок с «Открыть канал» /
«Я подписался». Два коммита нашего репозитория:

- **`aa0f81b`** — "Add subscription-gate to Mini App: require channel membership to use the bot"
- **`1c478b5`** — "Open the required channel inside Telegram instead of a browser" (маленький фикс поверх первого)

Всего 3 файла: `bot/api.py`, `bot/bot_sender.py`, `mini_app/index.html`.

## Идея

- Настройка — в обычной таблице `settings` (`required_channel_username`,
  username канала без `@`), редактируется через существующий
  `/api/admin/settings` и экран «Настройки» в админке — **новых таблиц нет**.
- **Пусто = гейт выключен.** После деплоя ничего не меняется, пока админ сам
  не впишет username канала.
- Проверка — `getChatMember` через Bot API. **Бота нужно вручную добавить
  админом в канал** (через сам Telegram, не через код) — иначе `getChatMember`
  не сможет проверять чужих участников.
- Админы (`ADMIN_IDS`/`SUPERADMIN_ID`) проходят без проверки — тот же паттерн
  бесплатного доступа, что и у whitelist везде в проекте.
- **Fail-open**: если запрос к Telegram упал (бот не админ канала, сеть и
  т.п.) — считаем подписанным, а не блокируем всех подряд из-за своей же
  ошибки конфигурации.
- Проверяется **один раз при открытии Mini App** (`GET /api/me`) + по кнопке
  «Я подписался». **Не** на каждый запрос, и **не** переотслеживается
  постоянно, если юзер отписался после того как получил доступ — осознанно
  отложено на потом, не забудьте про это при переносе.
- Гейтится только Mini App, бот-чат не трогали — по этому проекту вся
  функциональность и так внутри Mini App (проверьте, так ли у вас).

## 1. `bot/bot_sender.py` — обёртка над `getChatMember`

```python
import logging
from typing import Optional

import httpx
from config import settings
```

(добавить `from typing import Optional`, если его ещё нет)

```python
async def get_chat_member(chat_id: str, user_id: int) -> Optional[dict]:
    """Участник чата ({status, ...}) или None при сбое (бот не админ канала,
    канал не существует, сетевая ошибка и т.п.) — вызывающий код должен сам
    решить, как трактовать None (см. api.py::_check_subscription — fail-open)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BOT_API}/getChatMember",
                params={"chat_id": chat_id, "user_id": user_id},
            )
            data = r.json()
            if not data.get("ok"):
                logger.warning(f"bot_sender.get_chat_member: {data}")
                return None
            return data["result"]
    except Exception as e:
        logger.error(f"bot_sender.get_chat_member error: {e}")
        return None
```

## 2. `bot/api.py`

Импорт (расширить существующую строку с `send_message`/`send_document`):

```python
from bot_sender import (
    send_message as _bot_send,
    send_document as _bot_send_document,
    get_chat_member as _bot_get_chat_member,
)
```

Новая функция — сразу после `_get_admin`:

```python
async def _check_subscription(tg_id: int) -> dict:
    """Гейт «подпишись на канал, чтобы пользоваться ботом» — настраивается в
    админке (settings.required_channel_username, без @; пусто = гейт выключен).
    Админы проходят без проверки, как и everywhere else в проекте.

    Мягкая проверка на момент открытия Mini App (см. /api/me), НЕ на каждый
    запрос — постоянная перепроверка (юзер отписался после того как получил
    доступ) сознательно отложена на потом.

    При сбое запроса к Telegram (бот не админ канала, сеть и т.п.) — fail-open:
    считаем подписанным, чтобы наша же ошибка не заблокировала всех подряд.
    """
    channel = (await database.get_setting("required_channel_username") or "").strip().lstrip("@")
    if not channel:
        return {"channel": None, "subscribed": True}
    if settings.is_admin(tg_id):
        return {"channel": channel, "subscribed": True}

    member = await _bot_get_chat_member(f"@{channel}", tg_id)
    if member is None:
        logger.warning(f"subscription check failed for {tg_id} — failing open")
        return {"channel": channel, "subscribed": True}
    subscribed = member.get("status") in ("creator", "administrator", "member")
    return {"channel": channel, "subscribed": subscribed}
```

В `GET /api/me` — добавить вызов и два поля в ответ:

```python
@app.get("/api/me")
async def get_me(x_telegram_init_data: str = Header(None)):
    user     = await _get_user(x_telegram_init_data)
    prices   = await database.get_prices()
    packages = await database.get_token_packages()
    sub      = await _check_subscription(user["tg_id"])          # ← новое
    return {
        "tg_id":           user["tg_id"],
        "username":        user.get("username"),
        "full_name":       user.get("full_name"),
        "tenge_balance":   round(user.get("tenge_balance", 0.0), 2),
        "token_balance":   round(user.get("token_balance", 0.0), 2),
        "bonus_balance":   round(user.get("bonus_balance", 0.0), 2),
        "is_whitelisted":  bool(user.get("is_whitelisted")),
        "is_admin":        settings.is_admin(user["tg_id"]),
        "required_channel": sub["channel"],                       # ← новое
        "is_subscribed":   sub["subscribed"],                     # ← новое
        "prices":          prices,
        "premium_multiplier": await database.get_premium_multiplier(),
        "packages":        packages,
    }
```

В `EDITABLE_SETTINGS` — добавить ключ:

```python
EDITABLE_SETTINGS = {
    "turnitin_email", "turnitin_password", "turnitin_class_id", "turnitin_assign_id",
    "turnitin_class_id_premium", "turnitin_assign_id_premium", "premium_multiplier",
    "kaspi_phone", "kaspi_recipient_name",
    "help_username", "help_phone",
    "required_channel_username",   # ← новое
}
```

Ничего больше в `api.py` менять не нужно — `/api/admin/settings` (GET/POST)
уже общий для всех ключей из `EDITABLE_SETTINGS`, отдельный эндпоинт не нужен.

## 3. `mini_app/index.html`

### 3.1 Новое поле в экране «Настройки» (в массиве `FIELDS` компонента `AdminScreen`)

```jsx
{ key:'required_channel_username', label:'Обязательный канал (username без @, пусто = выключено)', type:'text' },
```

### 3.2 Новый компонент `SubscriptionGate` — вставить перед `function App()`

```jsx
// ── Гейт подписки на канал ───────────────────────────────────────────────────
function SubscriptionGate({ channel, checking, onRecheck }) {
  const openChannel = () => {
    const url = `https://t.me/${channel}`;
    // openTelegramLink — специально для t.me-ссылок, открывает канал внутри
    // самого Telegram; openLink увёл бы во внешний/встроенный браузер.
    if (tg?.openTelegramLink) tg.openTelegramLink(url);
    else if (tg?.openLink) tg.openLink(url);
    else window.open(url, '_blank');
  };
  return (
    <div className="screen" style={{display:'flex',flexDirection:'column',alignItems:'center',justifyContent:'center',minHeight:'100vh',textAlign:'center',gap:16,padding:'0 24px'}}>
      <div style={{width:64,height:64,borderRadius:20,background:'var(--accent-soft)',display:'flex',alignItems:'center',justifyContent:'center'}}>
        {React.cloneElement(ICONS.bell,{width:30,height:30,color:'var(--accent-2)'})}
      </div>
      <div>
        <h2 style={{marginBottom:8}}>Подпишись, чтобы пользоваться ботом</h2>
        <p style={{color:'var(--txt2)',fontSize:14,lineHeight:1.5,margin:0}}>
          Доступ ко всем функциям открыт только подписчикам канала <b>@{channel}</b>.
        </p>
      </div>
      <div style={{width:'100%',display:'flex',flexDirection:'column',gap:10,marginTop:8}}>
        <button className="btn btn-primary" onClick={openChannel}>Открыть канал</button>
        <button className="btn btn-secondary" style={{display:'flex',alignItems:'center',justifyContent:'center',gap:8}} onClick={onRecheck} disabled={checking}>
          {checking ? <><span className="spinner-sm" style={{borderTopColor:'var(--accent-2)'}}/> Проверяю…</> : 'Я подписался'}
        </button>
      </div>
    </div>
  );
}
```

Используются уже существующие в проекте `tg`, `ICONS.bell`, классы
`.screen/.btn/.btn-primary/.btn-secondary/.spinner-sm` — если у вас дизайн-
система совпадает (тот же файл-основа), ничего добавлять не нужно. Если
`ICONS.bell` нет — возьмите SVG из нашего `ICONS` в `index.html`.

### 3.3 Wiring в `App` — 3 маленьких правки

Добавить состояние (рядом с `loading`):

```jsx
const [checkingSub, setCheckingSub] = useState(false);
```

Добавить функцию перепроверки (после `useEffect` с `load()`):

```jsx
const recheckSubscription = async () => {
  setCheckingSub(true);
  try {
    const me = await api('/api/me');
    setUser(me);
    if (!me.is_subscribed) showToast('Пока не вижу подписку — попробуйте ещё раз через пару секунд');
  } catch(e) { showToast(e.message); }
  finally { setCheckingSub(false); }
};
```

Вставить гейт-ветку сразу после `if (loading) return ...`, до основного `return`:

```jsx
if (user?.required_channel && !user.is_subscribed) {
  return (
    <>
      <Toast msg={toast}/>
      <SubscriptionGate channel={user.required_channel} checking={checkingSub} onRecheck={recheckSubscription}/>
    </>
  );
}
```

## Как включить

1. Бота вручную добавить админом в нужный канал (через сам Telegram).
2. Админка → Настройки → «Обязательный канал» → username канала без `@`.
3. Готово — при следующем открытии Mini App неподписанные (кроме админов)
   увидят гейт.

Проверить руками: `getChatMember` для самого бота должен вернуть
`status: administrator` в этом канале — если нет, `_check_subscription`
будет тихо fail-open (никого не заблокирует, но и гейт не сработает) и в
логах будет `subscription check failed for ... — failing open`.

## Известный отложенный пробел

Проверка — только при открытии приложения, не постоянная. Юзер может
подписаться → получить доступ → отписаться, доступ сам не отзовётся.
Если понадобится — отдельная задача (например периодическая перепроверка
активных сессий или short-TTL кэш в `_check_subscription`).
