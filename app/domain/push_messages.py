"""Notification text for rank alerts, in the languages the app ships.

Copied from the app's own locale bundle (`src/lib/i18n/locales/*.json`,
keys `notificationRankChangeTitle` / `notificationRankChangeBody`) so a
pushed notification reads exactly like the one the app composes locally.

It lives here because a data-only message is not reliably displayed on iOS:
the title and body have to travel in the payload, which means the sender
needs them. Rank strings themselves are deliberately not translated -- the
app renders "Diamond 2" in every language.

`en-US` is the fallback for an unknown or unsupported locale.
"""

DEFAULT_LOCALE = "en-US"

# locale -> (title, body). `body` takes `name` and `rank`.
RANK_ALERT_MESSAGES: dict[str, tuple[str, str]] = {
    "de": (
        "Rang-Update",
        "{name} ist jetzt {rank}",
    ),
    "en-GB": (
        "Rank Update",
        "{name} is now {rank}",
    ),
    "en-US": (
        "Rank Update",
        "{name} is now {rank}",
    ),
    "es-MX": (
        "Actualización de rango",
        "{name} ahora es {rank}",
    ),
    "es": (
        "Actualización de rango",
        "{name} ahora es {rank}",
    ),
    "fr": (
        "Mise à jour du rang",
        "{name} est maintenant {rank}",
    ),
    "it": (
        "Aggiornamento del grado",
        "{name} ora è {rank}",
    ),
    "ja": (
        "ランク更新",
        "{name}は現在{rank}です",
    ),
    "ko": (
        "랭크 업데이트",
        "{name}님이 이제 {rank}입니다",
    ),
    "pl": (
        "Aktualizacja rangi",
        "{name} ma teraz {rank}",
    ),
    "pt-BR": (
        "Atualização de patente",
        "{name} agora é {rank}",
    ),
    "pt": (
        "Atualização de patente",
        "{name} agora é {rank}",
    ),
    "ru": (
        "Обновление ранга",
        "{name} теперь {rank}",
    ),
    "zh-TW": (
        "分級更新",
        "{name} 現在是 {rank}",
    ),
    "zh": (
        "段位更新",
        "{name} 现在是 {rank}",
    ),
}


def rank_alert_text(locale: str, name: str, rank: str) -> tuple[str, str]:
    """Title and body for one rank alert, in the closest language available.

    Falls back from an exact match (`pt-BR`) to the bare language (`pt`) to
    English, so a device reporting a locale the app does not ship still gets a
    readable notification rather than none.
    """
    entry = RANK_ALERT_MESSAGES.get(locale)
    if entry is None:
        entry = RANK_ALERT_MESSAGES.get(locale.split("-", maxsplit=1)[0])
    if entry is None:
        entry = RANK_ALERT_MESSAGES[DEFAULT_LOCALE]

    title, body = entry
    return title, body.replace("{name}", name).replace("{rank}", rank)
