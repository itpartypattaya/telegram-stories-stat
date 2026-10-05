"""Table headers and report phrases (en, ru)."""
from __future__ import annotations

STRINGS = {
    "en": {
        "n": "#", "time": "Time", "after": "After", "name": "Name", "nick": "Username", "reaction": "Reaction",
        "who": "Who", "date": "Date", "type": "Type", "caption": "Caption", "views": "Views",
        "reactions": "Reactions", "replies": "Replies", "forwards": "Forwards", "first_hour": "1st hour",
        "index": "vs usual", "story": "Story", "seen": "Seen", "share": "Share", "lag": "Typical lag",
        "last": "Last view", "first": "First view", "status": "Status", "hour": "Hour", "weekday": "Day",
        "bar": "", "metric": "Metric", "value": "Value", "id": "ID", "rule": "rule",
        "photo": "photo", "video": "video", "document": "file",
        "mutual": "mutual contact", "contact": "contact", "close_friend": "close friend", "other": "not a contact",
        "core": "core", "regular": "regular", "occasional": "occasional", "new": "new", "cooling": "cooling",
        "lost": "lost",
        "total": "Total", "median": "Median", "stories": "Stories", "unique_viewers": "Unique viewers",
        "new_viewers": "New viewers", "lost_viewers": "Stopped viewing", "active_30d": "Active audience (30 d)",
        "posted": "Posted", "reach": "Reach", "at": "after", "h": "h", "m": "m", "d": "d",
        "viewers_hidden": "Telegram did not return the viewer list for this story",
        "channel_viewers_hidden": "Telegram does not show who viewed channel stories — counts, reactions and "
                                  "reposts only",
        "young_note": "still collecting views: compared with earlier stories at the same age",
        "no_data": "No data yet.", "period": "Period", "list_note": "listed viewers",
        "story_header": "Story", "summary_header": "Stories summary", "people_header": "Audience",
        "hours_header": "When people watch", "days_header": "By weekday", "compare_header": "Comparison",
        "channel_header": "Channel stories", "more_rows": "more rows — full table in the CSV export",
        "pulse": "Pulse", "pulse_up": "above usual", "pulse_down": "below usual", "pulse_flat": "as usual",
        "digest": "Stories digest", "top": "Top stories", "best_hours": "Best hours to post",
        "new_core": "In the core, first seen in the last 60 days", "cooling_people": "Cooling down",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    },
    "ru": {
        "n": "№", "time": "Время", "after": "Через", "name": "Имя", "nick": "Ник", "reaction": "Реакция",
        "who": "Кто", "date": "Дата", "type": "Тип", "caption": "Подпись", "views": "Просмотры",
        "reactions": "Реакции", "replies": "Ответы", "forwards": "Пересылки", "first_hour": "1-й час",
        "index": "к обычному", "story": "Сторис", "seen": "Смотрел", "share": "Доля", "lag": "Обычно через",
        "last": "Последний", "first": "Первый", "status": "Статус", "hour": "Час", "weekday": "День",
        "bar": "", "metric": "Показатель", "value": "Значение", "id": "ID", "rule": "правило",
        "photo": "фото", "video": "видео", "document": "файл",
        "mutual": "взаимный контакт", "contact": "контакт", "close_friend": "близкий друг", "other": "не контакт",
        "core": "ядро", "regular": "регулярный", "occasional": "эпизодический", "new": "новый",
        "cooling": "остывает", "lost": "пропал",
        "total": "Итого", "median": "Медиана", "stories": "Сторис", "unique_viewers": "Уникальных зрителей",
        "new_viewers": "Новых зрителей", "lost_viewers": "Перестали смотреть",
        "active_30d": "Активная аудитория (30 дн)",
        "posted": "Опубликована", "reach": "Охват", "at": "через", "h": "ч", "m": "м", "d": "д",
        "viewers_hidden": "Telegram не отдал список зрителей этой сторис",
        "channel_viewers_hidden": "Кто смотрел сторис канала, Telegram не показывает — только счётчики, реакции "
                                  "и репосты",
        "young_note": "ещё набирает просмотры: сравнение с другими сторис в том же возрасте",
        "no_data": "Пока нет данных.", "period": "Период", "list_note": "зрителей в списке",
        "story_header": "Сторис", "summary_header": "Сводка по сторис", "people_header": "Аудитория",
        "hours_header": "Когда смотрят", "days_header": "По дням недели", "compare_header": "Сравнение",
        "channel_header": "Сторис канала", "more_rows": "строк ещё — полная таблица в CSV-выгрузке",
        "pulse": "Пульс", "pulse_up": "выше обычного", "pulse_down": "ниже обычного", "pulse_flat": "как обычно",
        "digest": "Сводка по сторис", "top": "Лучшие сторис", "best_hours": "Лучшее время публикации",
        "new_core": "В ядре, впервые смотрели за последние 60 дней", "cooling_people": "Остывают",
        "weekdays": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    },
}


def t(cfg_or_locale, key: str):
    locale = cfg_or_locale if isinstance(cfg_or_locale, str) else (cfg_or_locale or {}).get("locale", "en")
    table = STRINGS.get(locale) or STRINGS["en"]
    return table.get(key, STRINGS["en"].get(key, key))
