"""
DarkLook - Публичный Telegram бот для мониторинга профилей

ИНСТРУКЦИЯ ПО УСТАНОВКЕ:
1. pip install -r requirements.txt
2. Отредактируйте config.py (укажите BOT_TOKEN и ADMIN_ID)
3. python main.py

ДЕПЛОЙ НА БЕСПЛАТНЫЙ ХОСТИНГ:
- Render.com, Railway.app, Fly.io - см. README.md
"""

import asyncio
import logging
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from collections import defaultdict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated
from aiogram.exceptions import TelegramBadRequest

import config

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('darklook.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Rate limiting
user_last_command = {}
user_message_count = defaultdict(list)


class RateLimiter:
    """Защита от спама"""
    
    @staticmethod
    def check_cooldown(user_id: int) -> bool:
        """Проверка cooldown между командами"""
        now = datetime.now()
        if user_id in user_last_command:
            time_passed = (now - user_last_command[user_id]).total_seconds()
            if time_passed < config.COMMAND_COOLDOWN:
                return False
        user_last_command[user_id] = now
        return True
    
    @staticmethod
    def check_rate_limit(user_id: int) -> bool:
        """Проверка лимита сообщений"""
        now = datetime.now()
        # Очищаем старые записи
        user_message_count[user_id] = [
            msg_time for msg_time in user_message_count[user_id]
            if (now - msg_time).total_seconds() < config.RATE_LIMIT_PERIOD
        ]
        
        if len(user_message_count[user_id]) >= config.RATE_LIMIT_MESSAGES:
            return False
        
        user_message_count[user_id].append(now)
        return True


class Database:
    """Класс для работы с базой данных"""
    
    def __init__(self, db_name: str):
        self.db_name = db_name
    
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_name) as db:
            # Таблица пользователей бота
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица отслеживаемых пользователей
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tracked_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    target_user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP,
                    UNIQUE(owner_id, target_user_id)
                )
            """)
            
            # Таблица истории изменений
            await db.execute("""
                CREATE TABLE IF NOT EXISTS change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER,
                    target_user_id INTEGER,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица логов действий (для админа)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.commit()
            logger.info("База данных инициализирована")
    
    async def add_bot_user(self, user_id: int, username: str, first_name: str):
        """Добавление пользователя бота"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO bot_users (user_id, username, first_name, last_activity)
                    VALUES (?, ?, ?, ?)
                """, (user_id, username, first_name, datetime.now().isoformat()))
                await db.commit()
        except Exception as e:
            logger.error(f"Ошибка при добавлении пользователя бота: {e}")
    
    async def log_action(self, user_id: int, action: str, details: str = ""):
        """Логирование действий пользователей"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                await db.execute("""
                    INSERT INTO action_logs (user_id, action, details)
                    VALUES (?, ?, ?)
                """, (user_id, action, details))
                await db.commit()
        except Exception as e:
            logger.error(f"Ошибка при логировании: {e}")
    
    async def get_tracked_count(self, owner_id: int) -> int:
        """Получение количества отслеживаемых пользователей"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM tracked_users WHERE owner_id = ?",
                    (owner_id,)
                ) as cursor:
                    result = await cursor.fetchone()
                    return result[0] if result else 0
        except Exception as e:
            logger.error(f"Ошибка при подсчете: {e}")
            return 0
    
    async def add_tracked_user(self, owner_id: int, user_data: Dict) -> bool:
        """Добавление пользователя для отслеживания"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO tracked_users 
                    (owner_id, target_user_id, username, first_name, last_name, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    owner_id,
                    user_data['user_id'],
                    user_data['username'],
                    user_data['first_name'],
                    user_data['last_name'],
                    datetime.now().isoformat()
                ))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка при добавлении: {e}")
            return False
    
    async def remove_tracked_user(self, owner_id: int, target_user_id: int) -> bool:
        """Удаление пользователя из отслеживания"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                cursor = await db.execute(
                    "DELETE FROM tracked_users WHERE owner_id = ? AND target_user_id = ?",
                    (owner_id, target_user_id)
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка при удалении: {e}")
            return False
    
    async def get_tracked_users(self, owner_id: int = None) -> List[Dict]:
        """Получение списка отслеживаемых пользователей"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                db.row_factory = aiosqlite.Row
                if owner_id:
                    query = "SELECT * FROM tracked_users WHERE owner_id = ?"
                    params = (owner_id,)
                else:
                    query = "SELECT * FROM tracked_users"
                    params = ()
                
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка при получении списка: {e}")
            return []
    
    async def update_user_data(self, owner_id: int, target_user_id: int, 
                              field: str, new_value: str, old_value: str):
        """Обновление данных и запись в историю"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                await db.execute(
                    f"UPDATE tracked_users SET {field} = ?, last_checked = ? WHERE owner_id = ? AND target_user_id = ?",
                    (new_value, datetime.now().isoformat(), owner_id, target_user_id)
                )
                
                await db.execute("""
                    INSERT INTO change_history (owner_id, target_user_id, field_name, old_value, new_value)
                    VALUES (?, ?, ?, ?, ?)
                """, (owner_id, target_user_id, field, old_value, new_value))
                
                await db.commit()
        except Exception as e:
            logger.error(f"Ошибка при обновлении: {e}")
    
    async def get_all_bot_users(self) -> List[Dict]:
        """Получение всех пользователей бота (для админа)"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM bot_users ORDER BY started_at DESC") as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка при получении пользователей: {e}")
            return []
    
    async def get_recent_actions(self, limit: int = 20) -> List[Dict]:
        """Получение последних действий (для админа)"""
        try:
            async with aiosqlite.connect(self.db_name) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM action_logs ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Ошибка при получении логов: {e}")
            return []


class UserMonitor:
    """Класс для мониторинга изменений"""
    
    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.monitoring = False
    
    async def get_user_info(self, user_id: int) -> Optional[Dict]:
        """Получение информации о пользователе через Bot API"""
        try:
            chat = await self.bot.get_chat(user_id)
            
            return {
                'user_id': chat.id,
                'username': chat.username or '',
                'first_name': chat.first_name or '',
                'last_name': chat.last_name or '',
            }
        except TelegramBadRequest as e:
            logger.warning(f"Пользователь {user_id} недоступен: {e}")
            return None
        except Exception as e:
            logger.error(f"Ошибка при получении информации: {e}")
            return None
    
    async def check_changes(self):
        """Проверка изменений у всех отслеживаемых"""
        users = await self.db.get_tracked_users()
        
        for user in users:
            try:
                current_info = await self.get_user_info(user['target_user_id'])
                
                if not current_info:
                    continue
                
                changes = []
                fields_map = {
                    'username': 'Юзернейм',
                    'first_name': 'Имя',
                    'last_name': 'Фамилия'
                }
                
                for field, display_name in fields_map.items():
                    old_value = user[field] or ''
                    new_value = current_info[field] or ''
                    
                    if old_value != new_value:
                        changes.append({
                            'field': field,
                            'display_name': display_name,
                            'old': old_value,
                            'new': new_value
                        })
                        
                        await self.db.update_user_data(
                            user['owner_id'],
                            user['target_user_id'],
                            field,
                            new_value,
                            old_value
                        )
                
                if changes:
                    await self.send_change_notification(user['owner_id'], current_info['username'], changes)
                
            except Exception as e:
                logger.error(f"Ошибка при проверке: {e}")
    
    async def send_change_notification(self, owner_id: int, username: str, changes: List[Dict]):
        """Отправка уведомления об изменениях"""
        message = f"📢 <b>Изменения у @{username}:</b>\n\n"
        
        for change in changes:
            old_val = change['old'] if change['old'] else '<i>пусто</i>'
            new_val = change['new'] if change['new'] else '<i>пусто</i>'
            message += f"<b>{change['display_name']}:</b> {old_val} → {new_val}\n"
        
        try:
            await self.bot.send_message(owner_id, message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления: {e}")
    
    async def start_monitoring(self):
        """Запуск мониторинга"""
        self.monitoring = True
        logger.info("Мониторинг запущен")
        
        while self.monitoring:
            try:
                await self.check_changes()
                await asyncio.sleep(config.CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(config.CHECK_INTERVAL)


# Инициализация
db = Database(config.DB_NAME)
monitor = UserMonitor(bot, db)



# Middleware для логирования
@dp.message()
async def log_user_activity(message: Message):
    """Логирование активности пользователей"""
    await db.add_bot_user(
        message.from_user.id,
        message.from_user.username or '',
        message.from_user.first_name or ''
    )


# Обработчики команд
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    if not RateLimiter.check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите немного.")
        return
    
    await db.log_action(message.from_user.id, "start")
    
    welcome_text = """
🔍 <b>DarkLook - Мониторинг профилей Telegram</b>

Я помогу отслеживать изменения в профилях пользователей!

<b>Доступные команды:</b>

/track @username - начать отслеживание
/list - мои отслеживаемые пользователи
/stop @username - остановить отслеживание
/info @username - информация о пользователе

<b>Что я отслеживаю:</b>
• Изменение username
• Изменение имени
• Изменение фамилии

Проверка каждые 15 секунд ⏱
Максимум 5 пользователей на человека 👥
    """
    
    await message.answer(welcome_text, parse_mode='HTML')
    
    # Уведомление админу о новом пользователе
    try:
        await bot.send_message(
            config.ADMIN_ID,
            f"🆕 Новый пользователь:\n"
            f"ID: {message.from_user.id}\n"
            f"Username: @{message.from_user.username or 'нет'}\n"
            f"Имя: {message.from_user.first_name}"
        )
    except:
        pass


@dp.message(Command("track"))
async def cmd_track(message: Message):
    """Команда /track"""
    if not RateLimiter.check_cooldown(message.from_user.id):
        await message.answer("⏳ Подождите немного между командами")
        return
    
    if not RateLimiter.check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов. Подождите.")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Укажите username: /track @username")
        return
    
    username = parts[1].lstrip('@')
    
    # Проверка лимита
    count = await db.get_tracked_count(message.from_user.id)
    if count >= config.MAX_TRACKED_USERS_PER_USER:
        await message.answer(
            f"❌ Достигнут лимит: максимум {config.MAX_TRACKED_USERS_PER_USER} пользователей.\n"
            f"Удалите кого-то командой /stop @username"
        )
        return
    
    status_msg = await message.answer(f"🔍 Ищу @{username}...")
    
    # Пытаемся найти пользователя через поиск по username
    try:
        # Получаем информацию через Bot API
        # Примечание: Bot API не может получить user_id по username напрямую
        # Пользователь должен был хотя бы раз взаимодействовать с ботом
        
        # Для публичного бота: просим пользователя переслать сообщение
        await status_msg.edit_text(
            f"ℹ️ Чтобы отслеживать @{username}, попросите его:\n\n"
            f"1. Написать любое сообщение этому боту\n"
            f"2. Или перешлите мне любое сообщение от @{username}\n\n"
            f"После этого я смогу начать отслеживание!"
        )
        
        await db.log_action(
            message.from_user.id,
            "track_attempt",
            f"@{username}"
        )
        
        # Уведомление админу
        try:
            await bot.send_message(
                config.ADMIN_ID,
                f"🔍 Попытка отслеживания:\n"
                f"Пользователь: @{message.from_user.username or message.from_user.id}\n"
                f"Ищет: @{username}"
            )
        except:
            pass
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Ошибка в track: {e}")


@dp.message(F.forward_from)
async def handle_forward(message: Message):
    """Обработка пересланных сообщений"""
    if not RateLimiter.check_rate_limit(message.from_user.id):
        return
    
    forwarded_user = message.forward_from
    if not forwarded_user:
        return
    
    # Проверка лимита
    count = await db.get_tracked_count(message.from_user.id)
    if count >= config.MAX_TRACKED_USERS_PER_USER:
        await message.answer(
            f"❌ Достигнут лимит: максимум {config.MAX_TRACKED_USERS_PER_USER} пользователей"
        )
        return
    
    user_data = {
        'user_id': forwarded_user.id,
        'username': forwarded_user.username or '',
        'first_name': forwarded_user.first_name or '',
        'last_name': forwarded_user.last_name or ''
    }
    
    success = await db.add_tracked_user(message.from_user.id, user_data)
    
    if success:
        await message.answer(
            f"✅ <b>Пользователь добавлен!</b>\n\n"
            f"👤 Username: @{user_data['username']}\n"
            f"📝 Имя: {user_data['first_name']} {user_data['last_name']}\n"
            f"🆔 ID: {user_data['user_id']}\n\n"
            f"Я буду отслеживать изменения каждые 15 секунд!",
            parse_mode='HTML'
        )
        
        await db.log_action(
            message.from_user.id,
            "track_success",
            f"@{user_data['username']} (ID: {user_data['user_id']})"
        )
        
        # Уведомление админу
        try:
            await bot.send_message(
                config.ADMIN_ID,
                f"✅ Новое отслеживание:\n"
                f"Пользователь: @{message.from_user.username or message.from_user.id}\n"
                f"Отслеживает: @{user_data['username']} (ID: {user_data['user_id']})"
            )
        except:
            pass
    else:
        await message.answer("❌ Ошибка при добавлении")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    """Команда /list"""
    if not RateLimiter.check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов")
        return
    
    users = await db.get_tracked_users(message.from_user.id)
    
    if not users:
        await message.answer(
            "📋 У вас нет отслеживаемых пользователей\n\n"
            "Перешлите мне сообщение от пользователя, которого хотите отслеживать"
        )
        return
    
    text = f"📋 <b>Ваши отслеживаемые ({len(users)}/{config.MAX_TRACKED_USERS_PER_USER}):</b>\n\n"
    
    for user in users:
        text += f"👤 @{user['username'] or 'нет username'}\n"
        text += f"   Имя: {user['first_name']} {user['last_name']}\n"
        text += f"   ID: {user['target_user_id']}\n\n"
    
    await message.answer(text, parse_mode='HTML')
    await db.log_action(message.from_user.id, "list")



@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """Команда /stop"""
    if not RateLimiter.check_cooldown(message.from_user.id):
        await message.answer("⏳ Подождите между командами")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Укажите ID: /stop 123456789")
        return
    
    try:
        target_user_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Укажите числовой ID пользователя")
        return
    
    success = await db.remove_tracked_user(message.from_user.id, target_user_id)
    
    if success:
        await message.answer(f"✅ Пользователь удален из отслеживания")
        await db.log_action(message.from_user.id, "stop", f"ID: {target_user_id}")
    else:
        await message.answer(f"❌ Пользователь не найден в вашем списке")


@dp.message(Command("info"))
async def cmd_info(message: Message):
    """Команда /info"""
    if not RateLimiter.check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов")
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Укажите ID: /info 123456789")
        return
    
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("❌ Укажите числовой ID")
        return
    
    status_msg = await message.answer("🔍 Получаю информацию...")
    
    user_info = await monitor.get_user_info(user_id)
    
    if not user_info:
        await status_msg.edit_text("❌ Не удалось получить информацию")
        return
    
    info_text = f"""
📊 <b>Информация о пользователе</b>

👤 Username: @{user_info['username'] or 'нет'}
📝 Имя: {user_info['first_name']}
📝 Фамилия: {user_info['last_name']}
🆔 ID: {user_info['user_id']}
    """
    
    await status_msg.edit_text(info_text, parse_mode='HTML')
    await db.log_action(message.from_user.id, "info", f"ID: {user_id}")


# Админские команды
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ-панель"""
    if message.from_user.id != config.ADMIN_ID:
        return
    
    admin_text = """
👑 <b>Админ-панель DarkLook</b>

/stats - статистика бота
/users - список всех пользователей
/logs - последние действия
/broadcast - рассылка (в разработке)
    """
    
    await message.answer(admin_text, parse_mode='HTML')



@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика бота (только для админа)"""
    if message.from_user.id != config.ADMIN_ID:
        return
    
    bot_users = await db.get_all_bot_users()
    tracked = await db.get_tracked_users()
    
    stats_text = f"""
📊 <b>Статистика DarkLook</b>

👥 Всего пользователей: {len(bot_users)}
🔍 Всего отслеживаний: {len(tracked)}
📈 Среднее на пользователя: {len(tracked) / len(bot_users) if bot_users else 0:.1f}

<b>Последние 5 пользователей:</b>
    """
    
    for user in bot_users[:5]:
        stats_text += f"\n• @{user['username'] or 'нет'} (ID: {user['user_id']})"
    
    await message.answer(stats_text, parse_mode='HTML')


@dp.message(Command("users"))
async def cmd_users(message: Message):
    """Список всех пользователей (только для админа)"""
    if message.from_user.id != config.ADMIN_ID:
        return
    
    bot_users = await db.get_all_bot_users()
    
    if not bot_users:
        await message.answer("Пользователей пока нет")
        return
    
    text = f"👥 <b>Все пользователи ({len(bot_users)}):</b>\n\n"
    
    for user in bot_users[:50]:  # Первые 50
        text += f"• @{user['username'] or 'нет'} (ID: {user['user_id']})\n"
        text += f"  Имя: {user['first_name']}\n"
        text += f"  Начал: {user['started_at'][:10]}\n\n"
    
    if len(bot_users) > 50:
        text += f"\n... и еще {len(bot_users) - 50} пользователей"
    
    await message.answer(text, parse_mode='HTML')


@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    """Последние действия (только для админа)"""
    if message.from_user.id != config.ADMIN_ID:
        return
    
    logs = await db.get_recent_actions(20)
    
    if not logs:
        await message.answer("Логов пока нет")
        return
    
    text = "📝 <b>Последние действия:</b>\n\n"
    
    for log in logs:
        text += f"• ID {log['user_id']}: {log['action']}\n"
        if log['details']:
            text += f"  {log['details']}\n"
        text += f"  {log['created_at'][:16]}\n\n"
    
    await message.answer(text, parse_mode='HTML')


async def main():
    """Главная функция"""
    try:
        await db.init_db()
        
        # Запуск мониторинга в фоне
        monitoring_task = asyncio.create_task(monitor.start_monitoring())
        
        logger.info("Бот запущен")
        
        # Уведомление админу
        try:
            await bot.send_message(config.ADMIN_ID, "🚀 DarkLook запущен!")
        except:
            pass
        
        await dp.start_polling(bot)
        
    except KeyboardInterrupt:
        logger.info("Остановка бота")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа завершена")
