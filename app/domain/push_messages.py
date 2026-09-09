"""Notification text for push alerts, in the languages the app ships.

Copied from the app's own locale bundle (`src/lib/i18n/locales/*.json`,
keys `notificationRankChangeTitle` / `notificationRankChangeBody` and
`notificationHeroUpdateTitle` / `notificationHeroUpdateBody`) so a pushed
notification reads exactly like the one the app composes locally.

It lives here because a data-only message is not reliably displayed on iOS:
the title and body have to travel in the payload, which means the sender
needs them. Rank strings and hero names are deliberately not translated -- the
app renders "Diamond 2" and "D.Va" in every language.

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


# locale -> (title, body). `body` takes `heroes`, an already-joined list of
# display names. Deliberately plural-agnostic: "Changes to X" reads the same
# for one hero or three, so fifteen locales need no plural rules.
HERO_ALERT_MESSAGES: dict[str, tuple[str, str]] = {
    "de": (
        "Helden-Update",
        "Änderungen an {heroes} im neuesten Patch",
    ),
    "en-GB": (
        "Hero Update",
        "Changes to {heroes} in the latest patch",
    ),
    "en-US": (
        "Hero Update",
        "Changes to {heroes} in the latest patch",
    ),
    "es-MX": (
        "Actualización de héroes",
        "Cambios en {heroes} en el último parche",
    ),
    "es": (
        "Actualización de héroes",
        "Cambios en {heroes} en el último parche",
    ),
    "fr": (
        "Mise à jour des héros",
        "Changements pour {heroes} dans le dernier patch",
    ),
    "it": (
        "Aggiornamento eroi",
        "Modifiche a {heroes} nell'ultima patch",
    ),
    "ja": (
        "ヒーロー更新",
        "最新パッチで{heroes}が変更されました",
    ),
    "ko": (
        "영웅 업데이트",
        "최신 패치에서 {heroes} 변경됨",
    ),
    "pl": (
        "Aktualizacja bohaterów",
        "Zmiany dla {heroes} w najnowszej aktualizacji",
    ),
    "pt-BR": (
        "Atualização de heróis",
        "Mudanças em {heroes} no patch mais recente",
    ),
    "pt": (
        "Atualização de heróis",
        "Mudanças em {heroes} no patch mais recente",
    ),
    "ru": (
        "Обновление героев",
        "Изменения: {heroes} в последнем патче",
    ),
    "zh-TW": (
        "英雄更新",
        "最新更新中 {heroes} 有變動",
    ),
    "zh": (
        "英雄更新",
        "最新补丁中 {heroes} 有改动",
    ),
}


def _pick(messages: dict[str, tuple[str, str]], locale: str) -> tuple[str, str]:
    """The closest entry to *locale*: exact (`pt-BR`), bare (`pt`), English.

    A device reporting a locale the app does not ship still gets a readable
    notification rather than none.
    """
    return (
        messages.get(locale)
        or messages.get(locale.split("-", maxsplit=1)[0])
        or messages[DEFAULT_LOCALE]
    )


def rank_alert_text(locale: str, name: str, rank: str) -> tuple[str, str]:
    """Title and body for one rank alert, in the closest language available."""
    title, body = _pick(RANK_ALERT_MESSAGES, locale)
    return title, body.replace("{name}", name).replace("{rank}", rank)


def hero_alert_text(locale: str, heroes: list[str]) -> tuple[str, str]:
    """Title and body for one hero-change alert, in the closest language.

    *heroes* are display names, most-played first, already capped by the
    caller — the body names them all.
    """
    title, body = _pick(HERO_ALERT_MESSAGES, locale)
    return title, body.replace("{heroes}", ", ".join(heroes))
