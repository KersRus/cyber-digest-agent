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
]

DAYS_BACK = 7

SYSTEM_PROMPT = """
Ты — аналитик кибербезопасности. На вход подаётся список новостей.

Для каждой новости определи:

1. ОТНОСИТСЯ ЛИ ОНА К КИБЕРАТАКЕ НА КОМПАНИЮ В РФ?
   - Да → продолжи анализ
   - Нет → ИСКЛЮЧИ из результата

2. КАТЕГОРИЯ УЩЕРБА:
   - "business_disruption" — атака привела к нарушению бизнес-процессов,
     остановке производства, блокировке серверов, срыву операций,
     финансовым убыткам. ПРИОРИТЕТ 1.
   - "data_leak" — атака привела к утечке данных клиентов, сотрудников,
     коммерческой тайны. ПРИОРИТЕТ 2.
   - "other" — атака без явного ущерба (DDoS, дефейс, попытка без последствий).
     ПРИОРИТЕТ 3.

3. ЕСЛИ НОВОСТЕЙ НЕТ — верни пустой массив [].

ФОРМАТ ОТВЕТА (строго JSON-объект с единственным ключом "items",
значение — массив объектов):
{
  "items": [
    {
      "title": "...",
      "company": "...",
      "category": "business_disruption" | "data_leak" | "other",
      "summary": "1–2 предложения",
      "source_url": "...",
      "date": "YYYY-MM-DD"
    }
  ]
}

ВАЖНО: не выдумывай новости. Если информации недостаточно —
не включай запись в результат. Если новостей нет — верни {"items": []}.
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

    response = client.chat.completions.create(
        model=f"gpt://{folder_id}/yandexgpt-lite/latest",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("[WARN] LLM вернула не-JSON, пропускаем классификацию")
        return []

    if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        return parsed["items"]
    if isinstance(parsed, list):
        return parsed
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
