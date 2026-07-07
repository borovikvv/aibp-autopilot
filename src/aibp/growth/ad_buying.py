"""Semi-automatic ad-buying pipeline (issue #39).

`aibp ad-plan <donor_channel>` prepares everything for one ad buy:

  1. donor stats + forecast via TGStat (reuses competitor_monitor's
     build_recommendation: expected subscribers, max justified price);
     without a TGStat token the plan is still produced, just without numbers;
  2. a dedicated invite link (traffic source `ad_<channel>_<date>`) so every
     subscriber from this placement is attributed and actual CPS is computed;
  3. an LLM-written ad creative that ends with the tracked invite link, plus
     a ready-to-send application message to the donor channel's admin;
  4. a markdown plan in reports/ads/.

The human negotiates and pays (prohibited actions: покупка/перевод денег),
then records the outcome: `aibp source-set <slug> --cost ... --status live`.
The weekly growth report compares forecast vs actual CPS.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import structlog

from aibp.growth.competitor_monitor import (
    build_recommendation,
    fetch_competitor_stats,
    get_our_er_percent,
    load_growth_config,
)
from aibp.utils.config import PROJECT_ROOT

log = structlog.get_logger()

ADS_DIR = PROJECT_ROOT / "reports" / "ads"

CREATIVE_PROMPT = """Ты — редактор Telegram-канала @AI_Business_Pulse (практика внедрения ИИ \
в бизнес-процессы: кейсы, метрики, пилоты без хаоса; без хайпа и кликбейта).

Напиши рекламный пост для размещения в канале @{donor} (тематика: ИИ/технологии, РУ-аудитория).

Требования:
- 400–600 знаков, 2–3 абзаца, без эмодзи, без слов «шок», «срочно», «успей»;
- тон — спокойный экспертный, как у поста самого канала;
- конкретика: что читатель получает (разборы внедрений с цифрами, чек-листы пилотов);
- последняя строка — ровно этот призыв с ссылкой (не менять URL):
  Подписаться: {invite_link}

Ответь только текстом поста, без пояснений."""

APPLICATION_TEMPLATE = """Здравствуйте! Хотим разместить рекламный пост в @{donor}.

Наш канал: @AI_Business_Pulse — практика внедрения ИИ в бизнес-процессы \
(кейсы с метриками, пилоты, инструменты под конкретные сценарии).

Подскажите, пожалуйста:
1. Стоимость размещения 1/24 (или ваши форматы и цены)?
2. Ближайшие свободные даты?
3. Требования к креативу?

Креатив готов, пришлём сразу после согласования условий."""


def forecast_for_channel(username: str) -> dict | None:
    """Donor forecast via TGStat; None when no token / no data."""
    token = os.getenv("TGSTAT_API_TOKEN", "")
    if not token:
        return None
    stats = fetch_competitor_stats(username, token)
    if not stats:
        return None
    cfg = load_growth_config()
    return build_recommendation(
        stats,
        subscriber_value_rub=cfg["subscriber_value_rub"],
        assumed_conversion_pct=cfg["assumed_conversion_pct"],
        our_er_percent=get_our_er_percent(),
    )


def build_plan_md(slug: str, donor: str, invite_link: str, creative: str,
                  application: str, forecast: dict | None) -> str:
    """Assemble the ad-buy plan document."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"# Ad buy plan — @{donor} ({today})", "",
             f"Источник трафика: `{slug}`",
             f"Invite-ссылка (все подписки атрибуцируются): {invite_link}", ""]

    lines.append("## Прогноз")
    if forecast:
        verdict = "✅ стоит рассмотреть" if forecast["worth_buying"] else "❌ по метрикам не стоит"
        lines += [
            f"- Вердикт: {verdict}",
            f"- Ожидаемый приток: ~{forecast['expected_subscribers_per_post']} подписчиков",
            f"- Максимальная оправданная цена: **{forecast['max_justified_price_rub']} ₽**",
            f"- Заметки: {forecast['notes']}",
        ]
    else:
        lines.append("- Нет данных TGStat (нет токена или канал не найден) — "
                     "запросить у админа охват поста и посчитать вручную: "
                     "цена ≤ охват × 1% × 50 ₽.")
    lines += ["", "## Креатив", "", creative, "",
              "## Заявка админу", "", application, "",
              "## После размещения", "",
              f"1. `aibp source-set {slug} --cost <₽> --status live` — зафиксировать цену;",
              "2. фактический CPS появится в еженедельном growth-отчёте "
              "(подписки по invite-ссылке считаются автоматически);",
              f"3. по итогам — `aibp source-set {slug} --status done`.", "",
              "_Оплата и договорённости — вручную; система только измеряет._"]
    return "\n".join(lines)


def plan_ad_buy(donor: str) -> str:
    """Prepare a full ad-buy plan for a donor channel. Returns the plan path."""
    donor = donor.lstrip("@")
    slug = f"ad_{donor}_{datetime.now(UTC).strftime('%Y%m%d')}"

    forecast = forecast_for_channel(donor)

    from aibp.growth.traffic_sources import create_source
    source = create_source(
        slug,
        kind="ad_buy",
        channel_username=donor,
        expected_subscribers=(forecast or {}).get("expected_subscribers_per_post"),
        expected_cps_rub=(forecast or {}).get("estimated_cac_at_max_price_rub"),
        notes=f"ad buy in @{donor}",
    )
    invite_link = source["invite_link"]

    from aibp.enrichment.llm_client import OpenRouterClient
    creative = OpenRouterClient().chat(
        [{"role": "user",
          "content": CREATIVE_PROMPT.format(donor=donor, invite_link=invite_link)}],
        temperature=0.7,
        max_tokens=1000,
    ).strip()
    if invite_link not in creative:
        # The tracked link is the whole point — never let the LLM drop it.
        creative = f"{creative}\n\nПодписаться: {invite_link}"

    plan = build_plan_md(slug, donor, invite_link, creative,
                         APPLICATION_TEMPLATE.format(donor=donor), forecast)

    ADS_DIR.mkdir(parents=True, exist_ok=True)
    path = ADS_DIR / f"{slug}.md"
    path.write_text(plan, encoding="utf-8")
    log.info("ad_plan_ready", donor=donor, slug=slug, path=str(path))
    return str(path)
