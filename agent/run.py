import os
import json
import feedparser
from datetime import datetime, timedelta, timezone
from openai import OpenAI

# ──────────────────────────────────────────────
# 1. КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────

RSS_FEEDS = [
    "https://www.anti-malware.ru/news/feed",
    "https://www.securitylab.ru/rss/",
    "https://safe.cnews.ru/rss/",
    "https://www.tadviser.ru/rss/",
    "https://www.kommersant.ru/rss/",
    "https://xakep.ru/feed/",
    "https://habr.com/ru/rss/hubs/infosecurity/all/",
    "https://www.securitylab.ru/_Services/Export/RSS/news/"
]

DAYS_BACK = 7

SYSTEM_PROMPT = """
Ты — аналитик кибербезопасности. На вход подаётся JSON-массив новостей
из российских ИБ-СМИ. Твоя задача — отобрать те, что описывают
кибератаки на компании в РФ, и классифицировать их.

Для каждой новости:

1. ОТНОСИТСЯ ЛИ ОНА К КИБЕРАТАКЕ НА КОМПАНИЮ В РФ?
   - Да → включи в результат
   - Нет (мировые новости, общие обзоры, релизы продуктов,
     законопроекты без инцидента) → пропусти

2. КАТЕГОРИЯ (выбери одну):
   - "business_disruption" — атака нарушила бизнес-процессы:
     остановка производства, блокировка серверов, срыв операций,
     финансовые убытки, простой. ПРИОРИТЕТ 1.
   - "data_leak" — атака привела к утечке данных клиентов,
     сотрудников, коммерческой тайны. ПРИОРИТЕТ 2.
   - "other" — прочие атаки: DDoS, дефейс, фишинг без явных
     последствий, попытки без успеха. ПРИОРИТЕТ 3.

3. Если сомневаешься между категориями — выбирай более мягкую
   (data_leak вместо business_disruption, other вместо data_leak).
   НО НЕ ВЫБРАСЫВАЙ новость только потому, что детали неполные —
   если это явно кибератака на российскую компанию, включи её.

ВАЖНО: лучше включить новость с категорией "other", чем потерять
реальный инцидент. Исключай только явно нерелевантное
(мировые новости, обзоры рынка, релизы продуктов).

ФОРМАТ ОТВЕТА — строго JSON-объект:
{
  "items": [
    {
      "title": "заголовок новости",
      "company": "название компании или 'не указана'",
      "category": "business_disruption" | "data_leak" | "other",
      "summary": "1–2 предложения о сути инцидента",
      "source_url": "ссылка из входных данных",
      "date": "YYYY-MM-DD"
    }
  ]
}

Если новостей, подходящих под критерии, нет — верни {"items": []}.
Не выдумывай данные. Бери только то, что есть во входном JSON.
"""

# ──────────────────────────────────────────────
# 2. СБОР RSS
# ──────────────────────────────────────────────

def fetch_news():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    all_items = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published = entry.get("published_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                else:
                    pub_dt = datetime.now(timezone.utc)

                all_items.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", ""),
                    "published": pub_dt.strftime("%Y-%m-%d"),
                })
        except Exception as e:
            print(f"[WARN] Не удалось прочитать {url}: {e}")

    seen = set()
    unique = []
    for item in all_items:
        if item["link"] not in seen:
            seen.add(item["link"])
            unique.append(item)

    print(f"[INFO] Собрано {len(unique)} уникальных новостей за {DAYS_BACK} дней")
    return unique

# ──────────────────────────────────────────────
# 3. КЛАССИФИКАЦИЯ ЧЕРЕЗ YANDEXGPT
# ──────────────────────────────────────────────

def classify_news(news_items):
    if not news_items:
        return []

    folder_id = os.environ["YANDEX_FOLDER_ID"]

    client = OpenAI(
        api_key=os.environ["YANDEX_API_KEY"],
        base_url="https://llm.api.cloud.yandex.net/v1",
        project=folder_id,
    )

    user_content = json.dumps(news_items, ensure_ascii=False, indent=2)
    # ─── ОТЛАДКА ВХОДА ───
    print(f"[DEBUG] Отправляем в модель {len(news_items)} новостей")
    print("[DEBUG] Пример первой новости:")
    print(json.dumps(news_items[0], ensure_ascii=False, indent=2)[:500])
    # ────────────────────
    response = client.chat.completions.create(
        model=f"gpt://{folder_id}/yandexgpt/latest",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    # ─── ОТЛАДКА ───
    print("[DEBUG] Сырой ответ модели (первые 3000 символов):")
    print(raw[:3000])
    print(f"[DEBUG] Длина ответа: {len(raw)} символов")
    print(f"[DEBUG] Finish reason: {response.choices[0].finish_reason}")
    # ────────────────

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[WARN] LLM вернула не-JSON: {e}")
        return []

    print(f"[DEBUG] Тип распарсенного объекта: {type(parsed).__name__}")
    if isinstance(parsed, dict):
        print(f"[DEBUG] Ключи словаря: {list(parsed.keys())}")

    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return parsed["items"]
    if isinstance(parsed, list):
        return parsed
    print("[WARN] Не удалось извлечь массив items из ответа LLM")
    return []

# ──────────────────────────────────────────────
# 4. СОРТИРОВКА И ФОРМИРОВАНИЕ HTML
# ──────────────────────────────────────────────

CATEGORY_ORDER = {
    "business_disruption": 0,
    "data_leak": 1,
    "other": 2,
}

CATEGORY_TITLES = {
    "business_disruption": "🔴 Нарушение бизнес-процессов / убытки",
    "data_leak": "🟠 Утечка данных",
    "other": "🟡 Прочие инциденты",
}

def build_digest(classified):
    if not classified:
        return None

    classified.sort(key=lambda x: (
        CATEGORY_ORDER.get(x.get("category", "other"), 99),
        x.get("date", ""),
    ))

    groups = {}
    for item in classified:
        cat = item.get("category", "other")
        groups.setdefault(cat, []).append(item)

    today = datetime.now().strftime("%d.%m.%Y")
    html_parts = [
        f"<h2>🛡 Дайджест кибератак на компании РФ — {today}</h2>",
        f"<p>Всего инцидентов за неделю: <b>{len(classified)}</b></p>",
        "<hr>",
    ]

    for cat_key in ["business_disruption", "data_leak", "other"]:
        items = groups.get(cat_key, [])
        if not items:
            continue
        html_parts.append(f"<h3>{CATEGORY_TITLES[cat_key]}</h3><ul>")
        for it in items:
            title = it.get("title", "Без названия")
            company = it.get("company", "")
            summary = it.get("summary", "")
            url = it.get("source_url", "#")
            date = it.get("date", "")
            html_parts.append(
                f"<li><b>{company}</b> — "
                f'<a href="{url}">{title}</a> '
                f"<i>({date})</i><br>{summary}</li>"
            )
        html_parts.append("</ul>")

    html_parts.append("<hr><p><i>Сформировано автоматически.</i></p>")
    return "\n".join(html_parts)

# ──────────────────────────────────────────────
# 5. MAIN
# ──────────────────────────────────────────────

def main():
    news = fetch_news()
    classified = classify_news(news)
    digest_html = build_digest(classified)

    if digest_html is None:
        print("[INFO] Новостей нет — письмо не будет отправлено")
        return

    with open("digest.html", "w", encoding="utf-8") as f:
        f.write(digest_html)
    print(f"[INFO] Дайджест сформирован: {len(classified)} записей")

if __name__ == "__main__":
    main()
