"""
football_config.py
لیست سفید لیگ‌ها و تیم‌ها (بخش ۷.۴) — فایل تنظیمات مستقل و قابل ویرایش.
نام‌ها/aliasها فقط برای تطبیق استفاده می‌شوند؛ `team_id` واقعی همیشه از
پاسخ معتبر Bzzoiro resolve و cache می‌شود، نه از اینجا hard-code.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

# نام رقابت همان‌طور که در پاسخ Bzzoiro/Football-Data می‌آید (برای تطبیق،
# بزرگ/کوچک بودن حروف نادیده گرفته می‌شود) -> نام فارسی برای نمایش.
ALLOWED_LEAGUES_FA: Dict[str, str] = {
    "uefa champions league": "لیگ قهرمانان اروپا",
    "champions league": "لیگ قهرمانان اروپا",
    "premier league": "لیگ برتر انگلیس",
    "la liga": "لالیگا اسپانیا",
    "bundesliga": "بوندس‌لیگا آلمان",
    "serie a": "سری آ ایتالیا",
    "süper lig": "سوپرلیگ ترکیه",
    "super lig": "سوپرلیگ ترکیه",
    "ligue 1": "لیگ ۱ فرانسه",
}

# alias های نام تیم -> نام کانونی (برای رفع اختلاف املایی/زبان در تطبیق متنی).
TEAM_ALIASES: Dict[str, str] = {
    "man city": "manchester city", "man utd": "manchester united", "man united": "manchester united",
    "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "newcastle": "newcastle united", "villa": "aston villa",
    "real madrid cf": "real madrid", "fc barcelona": "barcelona", "atletico madrid": "atlético madrid",
    "atleti": "atlético madrid",
    "bayern munich": "bayern münchen", "fc bayern": "bayern münchen", "dortmund": "borussia dortmund",
    "bvb": "borussia dortmund", "leverkusen": "bayer leverkusen",
    "inter milan": "inter", "ac milan": "ac milan", "milan": "ac milan", "juve": "juventus",
    "gala": "galatasaray", "fener": "fenerbahçe", "besiktas": "beşiktaş",
    "psg": "paris saint-germain", "om": "marseille", "asm": "monaco", "ol": "lyon",
}

SELECTED_TEAMS_CANONICAL: FrozenSet[str] = frozenset({
    # انگلیس
    "manchester city", "manchester united", "liverpool", "arsenal", "chelsea",
    "tottenham hotspur", "newcastle united", "aston villa",
    # اسپانیا
    "real madrid", "barcelona", "atlético madrid",
    # آلمان
    "bayern münchen", "borussia dortmund", "bayer leverkusen",
    # ایتالیا
    "inter", "ac milan", "juventus", "napoli", "roma",
    # ترکیه
    "galatasaray", "fenerbahçe", "beşiktaş",
    # فرانسه
    "paris saint-germain", "marseille", "monaco", "lyon",
})

MAX_MATCHES_PER_LOCAL_DAY = 10
FOOTBALL_LOOKAHEAD_DAYS_DEFAULT = 7
FOOTBALL_POLL_INTERVAL_MINUTES_DEFAULT = 15
FOOTBALL_TIMEZONE_DEFAULT = "Asia/Tehran"
SEND_BEFORE_KICKOFF_MINUTES = 90


def canonical_team_name(raw_name: str) -> str:
    key = (raw_name or "").strip().lower()
    return TEAM_ALIASES.get(key, key)


def is_selected_team(raw_name: str) -> bool:
    return canonical_team_name(raw_name) in SELECTED_TEAMS_CANONICAL


def canonical_league_fa(raw_league_name: str) -> str:
    key = (raw_league_name or "").strip().lower()
    return ALLOWED_LEAGUES_FA.get(key, "")


def is_allowed_league(raw_league_name: str) -> bool:
    key = (raw_league_name or "").strip().lower()
    return key in ALLOWED_LEAGUES_FA


def selection_priority(raw_league_name: str, home_team: str, away_team: str) -> Tuple[int, int]:
    """امتیاز فقط برای انتخاب استفاده می‌شود؛ هرگز به کاربر نمایش داده
    نمی‌شود. عدد کوچک‌تر = اولویت بالاتر (برای sort صعودی).
    رتبه ۰: هر دو تیم منتخب. رتبه ۱: یکی منتخب. رتبه ۲: در allowlist نیست
    (نباید اصلاً اینجا برسد، ولی fallback ایمن)."""
    both_selected = is_selected_team(home_team) and is_selected_team(away_team)
    one_selected = is_selected_team(home_team) or is_selected_team(away_team)
    team_rank = 0 if both_selected else (1 if one_selected else 2)
    league_rank = list(ALLOWED_LEAGUES_FA.keys()).index((raw_league_name or "").strip().lower()) \
        if is_allowed_league(raw_league_name) else 99
    return (team_rank, league_rank)
