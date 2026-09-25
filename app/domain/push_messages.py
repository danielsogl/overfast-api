"""Notification text for push alerts, in the languages the app ships.

The rank and hero catalogues are copied from the app's own locale bundle
(`src/lib/i18n/locales/*.json`, keys `notificationRankChangeTitle` /
`notificationRankChangeBody` and `notificationHeroUpdateTitle` /
`notificationHeroUpdateBody`) so a pushed notification reads exactly like the
one the app composes locally. `WEEKLY_RECAP_MESSAGES` has no such source --
the recap is new, the app bundle carries no strings for it, and these
translations are ours alone.

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


def _pick[T: tuple[str, ...]](messages: dict[str, T], locale: str) -> T:
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


# locale -> (title, stats, top_hero). `stats` takes `name`, `games`, `wins` and
# is always present; `top_hero` takes `hero` and is appended only when the
# week had one. A rank move is appended after both, untranslated like every
# other rank string, so neither template mentions it. Deliberately built from
# counts rather than "X games, Y wins" prose: "Games: 1" reads fine where
# "1 games" would not, so fifteen locales need no plural rules here either.
WEEKLY_RECAP_MESSAGES: dict[str, tuple[str, str, str]] = {
    "de": (
        "Wochenrückblick",
        "{name}: Spiele: {games} · Siege: {wins}",
        "Meistgespielt: {hero}",
    ),
    "en-GB": (
        "Weekly Recap",
        "{name}: Games: {games} · Wins: {wins}",
        "Top hero: {hero}",
    ),
    "en-US": (
        "Weekly Recap",
        "{name}: Games: {games} · Wins: {wins}",
        "Top hero: {hero}",
    ),
    "es-MX": (
        "Resumen semanal",
        "{name}: Partidas: {games} · Victorias: {wins}",
        "Héroe más jugado: {hero}",
    ),
    "es": (
        "Resumen semanal",
        "{name}: Partidas: {games} · Victorias: {wins}",
        "Héroe más jugado: {hero}",
    ),
    "fr": (
        "Récap hebdomadaire",
        "{name} : Parties : {games} · Victoires : {wins}",
        "Héros le plus joué : {hero}",
    ),
    "it": (
        "Riepilogo settimanale",
        "{name}: Partite: {games} · Vittorie: {wins}",
        "Eroe più giocato: {hero}",
    ),
    "ja": (
        "週間レポート",
        "{name}：試合数 {games}・勝利 {wins}",
        "最多プレイ: {hero}",
    ),
    "ko": (
        "주간 리캡",
        "{name}: 경기 {games} · 승리 {wins}",
        "최다 플레이 영웅: {hero}",
    ),
    "pl": (
        "Podsumowanie tygodnia",
        "{name}: Mecze: {games} · Zwycięstwa: {wins}",
        "Najczęściej grany: {hero}",
    ),
    "pt-BR": (
        "Resumo semanal",
        "{name}: Partidas: {games} · Vitórias: {wins}",
        "Herói mais jogado: {hero}",
    ),
    "pt": (
        "Resumo semanal",
        "{name}: Partidas: {games} · Vitórias: {wins}",
        "Herói mais jogado: {hero}",
    ),
    "ru": (
        "Итоги недели",
        "{name}: Игры: {games} · Победы: {wins}",
        "Больше всего сыграно: {hero}",
    ),
    "zh-TW": (
        "每週回顧",
        "{name}：場次 {games}・勝場 {wins}",
        "最常使用英雄：{hero}",
    ),
    "zh": (
        "每周回顾",
        "{name}：场次 {games}・胜场 {wins}",
        "最常使用英雄：{hero}",
    ),
}


def weekly_recap_text(
    locale: str,
    name: str,
    games: int,
    wins: int,
    hero: str | None,
    rank: str | None,
) -> tuple[str, str]:
    """Title and body for one weekly recap, in the closest language available.

    *hero* is a display name already resolved by the caller, or None when the
    week carried no hero playtime. *rank* is an already-formatted move (see
    ``format_rank`` / ``push_alerts.weekly_recap``), or None when the highest
    rank did not change over the week. Both are optional trailing parts, so a
    quiet week on either front still reads as one clean sentence.
    """
    title, stats, top_hero = _pick(WEEKLY_RECAP_MESSAGES, locale)
    parts = [
        stats.replace("{name}", name)
        .replace("{games}", str(games))
        .replace("{wins}", str(wins))
    ]
    if hero:
        parts.append(top_hero.replace("{hero}", hero))
    if rank:
        parts.append(rank)
    return title, " · ".join(parts)
