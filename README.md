# University Course Registration Bot / Университетский Бот Регистрации на Курсы

[English](#english) | [Русский](#русский)

---

## English

A Telegram bot for managing university course registration queues with individual per-course scheduled opening times and Russian language interface.

### Features

- 🎓 **Per-Course Registration**: Each course has its own registration queue and schedule
- ⏰ **Individual Schedules**: Each course opens at its own configured day/time
- 📋 **Smart Queue Management**: Position tracking with duplicate prevention
- 📊 **Real-time Status**: Live queue monitoring with course availability
- 🔧 **Advanced Admin Tools**: Complete course and queue management
- 💾 **Persistent Storage**: JSON-based data persistence
- 🌐 **Russian Interface**: All user messages in Russian for better UX
- 🚀 **Network Resilience**: Robust error handling for stable operation

### Course Management

- **Dynamic Course Addition**: Admins can add courses with custom schedules
- **Flexible Scheduling**: Each course can have different registration days/times
- **Selective Queue Clearing**: Clear specific course queues or all at once
- **Registration Control**: Open/close registration for individual courses

### Setup

#### 1. Prerequisites

Python 3.13+ and required packages:

```bash
pip install python-telegram-bot==22.4 python-dotenv==1.1.1 pytz==2025.2 apscheduler==3.11.0
```

#### 2. Bot Configuration

1. Create a new bot with [@BotFather](https://t.me/BotFather)
2. Copy your bot token to `.env` file
3. Configure courses in `config.json` with individual schedules
4. Set admin user IDs in configuration

#### 3. Environment Variables & Security

Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
```

Edit `.env` with your configuration:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
DEV_USER_IDS=your_telegram_user_id_here
DEBUG=True
```

**Security Note**: 
- ✅ **Secure**: Admin/dev user IDs are stored in `.env` (not tracked by git)
- ✅ **Safe for public repos**: `.env` is ignored by git, `.env.example` provides template
- ❌ **Never commit**: Don't commit real `.env` files with tokens/user IDs to git
- 🔍 **Get your user ID**: Send `/start` to [@userinfobot](https://t.me/userinfobot) on Telegram

**Multiple Dev Users**: Use comma-separated format: `DEV_USER_IDS=123456789,987654321`

Note: Schedule configuration is now per-course in `config.json`, not global.

#### 4. Course Configuration

The bot uses `config.json` for course configuration. Admin user IDs are now securely stored in `.env`:

```json
{
  "groups": {
    "-1001234567890": {
      "name": "Computer Science Group",
      "created_at": "2025-09-20T10:00:00.000000",
      "courses": {
        "oop": {
          "name": "ООП ЛАБ",
          "schedule": {"day": 0, "time": "18:00"}
        },
        "comp": {
          "name": "ЦВМ ЛАБ", 
          "schedule": {"day": 2, "time": "20:00"}
        }
      }
    }
  },
  "group_admins": {
    "-1001234567890": [123456789]
  },
  "max_queue_size": 50
}
```

**Note**: Dev users with full access are now configured in `.env` file for security, while group-specific admins remain in `config.json`.

Days: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday

#### 5. Running the Bot

```bash
python main.py
```

### Usage

#### User Commands (Russian Interface)

- `/start` - Показать приветствие и команды
- `/list` - Показать все курсы и расписания  
- `/register` - Записать имя в список курса
- `/unregister` - Удалить регистрации
- `/status` - Посмотреть статус очереди
- `/myregistrations` - Показать ваши регистрации
- `/help` - Показать помощь

#### Admin Commands

- `/admin_open` - Open registration for specific courses
- `/admin_close` - Close registration for specific courses
- `/admin_clear` - Clear specific course queue
- `/admin_clear_all` - Clear all queues
- `/admin_status` - Detailed system status
- `/admin_config` - View current configuration
- `/admin_swap` - Swap positions in queue
- `/admin_add_course` - Add new course with schedule
- `/admin_remove_course` - Remove course

### How It Works

1. **Per-Course Scheduling**: Each course opens automatically based on its individual schedule
2. **Course Selection**: Users select from available open courses
3. **Name Registration**: Multiple names can be registered per course
4. **Queue Management**: Each course maintains its own queue with position tracking
5. **Duplicate Prevention**: Same name cannot appear twice in the same course queue

### File Structure

```
tgbot3/
├── main.py              # Main bot application
├── config.json          # Per-course configuration with schedules
├── requirements.txt     # Python dependencies
├── .env                # Bot token and debug settings
├── queue_data.json     # Persistent queue storage (auto-created)
├── README.md           # This documentation
└── DEPLOYMENT.md       # Deployment guide
```

### Data Storage

Queue data structure:

```json
{
  "queues": {
    "course_id": [
      {
        "user_id": 12345,
        "username": "student_username", 
        "full_name": "Student Name",
        "registered_at": "2025-09-19T20:00:00+03:00",
        "position": 1
      }
    ]
  },
  "course_registration_status": {
    "course_id": false
  },
  "last_updated": "2025-09-19T20:00:00+03:00"
}
```

### Network Reliability

The bot includes comprehensive error handling:
- Connection timeouts (30s) 
- Automatic retry for network failures
- Graceful handling of Telegram API issues
- Continues polling despite temporary network problems

---

## Русский

Telegram бот для управления очередями регистрации на университетские курсы с индивидуальными расписаниями для каждого курса.

### Возможности

- 🎓 **Регистрация по Курсам**: У каждого курса своя очередь и расписание
- ⏰ **Индивидуальные Расписания**: Каждый курс открывается в свое время
- 📋 **Умное Управление Очередями**: Отслеживание позиций с предотвращением дублирования
- 📊 **Статус в Реальном Времени**: Мониторинг очередей и доступности курсов
- 🔧 **Инструменты Администратора**: Полное управление курсами и очередями
- 💾 **Постоянное Хранение**: Сохранение данных в JSON формате
- 🌐 **Русский Интерфейс**: Все сообщения на русском языке
- 🚀 **Сетевая Устойчивость**: Надежная обработка сетевых ошибок

### Команды Пользователя

- `/start` - Показать приветствие и список команд
- `/list` - Показать все доступные курсы с расписаниями
- `/register` - Записаться на открытые курсы  
- `/unregister` - Отменить свои регистрации
- `/status` - Посмотреть текущий статус очередей
- `/myregistrations` - Показать ваши регистрации
- `/help` - Показать справку

### Команды Администратора

- `/admin_open` - Открыть регистрацию для курсов
- `/admin_close` - Закрыть регистрацию для курсов  
- `/admin_clear` - Очистить очередь конкретного курса
- `/admin_clear_all` - Очистить все очереди
- `/admin_status` - Подробный статус системы
- `/admin_config` - Посмотреть текущую конфигурацию
- `/admin_swap` - Поменять местами позиции в очереди
- `/admin_add_course` - Добавить новый курс с расписанием
- `/admin_remove_course` - Удалить курс

### Установка и Запуск

1. **Установите зависимости**:
   ```bash
   pip install python-telegram-bot==22.4 python-dotenv==1.1.1 pytz==2025.2 apscheduler==3.11.0
   ```

2. **Настройте бота**:
   - Создайте бота через [@BotFather](https://t.me/BotFather)
   - Добавьте токен в файл `.env`
   - Настройте курсы в `config.json`

3. **Запустите бота**:
   ```bash
   python main.py
   ```

### Как Это Работает

1. **Автоматическое Открытие**: Каждый курс открывается по своему расписанию
2. **Выбор Курса**: Пользователи выбирают из доступных открытых курсов
3. **Запись Имен**: Можно записать несколько человек на один курс
4. **Управление Очередями**: Каждый курс ведет свою очередь с позициями
5. **Предотвращение Дублей**: Одно имя не может появиться дважды в одном курсе

### Конфигурация Курсов

Пример `config.json`:

```json
{
  "courses": {
    "oop": {
      "name": "ООП ЛАБ",
      "schedule": {"day": 0, "time": "18:00"}
    },
    "comp": {
      "name": "ЦВМ ЛАБ",
      "schedule": {"day": 2, "time": "20:00"}
    }
  },
  "admins": [123456789]
}
```

Дни недели: 0=Понедельник, 1=Вторник, 2=Среда, 3=Четверг, 4=Пятница, 5=Суббота, 6=Воскресенье

### Лицензия

Проект с открытым исходным кодом. Свободно модифицируйте под нужды вашего университета.