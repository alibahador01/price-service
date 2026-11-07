"""
central_scheduler.py
طبق درخواست صریح کاربر برای بهینه‌سازی منابع Render: **یک Scheduler مرکزی
و سبک** به‌جای چند Thread/Worker جداگانه برای هر قابلیت جدید. این فایل
جایگزین poster_loop قدیمی می‌شود و مسئول هر هفت نوع پیام روزانه
(small_list, big_list, crypto_intel, market_pulse, whale_alert, arbitrage,
goodnight) + دیسپچ فوتبال (که به‌خاطر دقت ۹۰دقیقه‌ای نیاز به tick کوتاه
دارد) است — همه در یک حلقه واحد با یک sleep، بدون Thread اضافه.

هیچ تابعی در این فایل مستقیماً شبکه صدا نمی‌زند؛ همه چیز از طریق context
(دیکشنری از callableها، توسط render_app.py ساخته می‌شود) تزریق می‌شود تا
هم بدون اینترنت تست شود و هم به سیستم قیمت‌دهی ووچر/دلار/یوتوپیا دست
نخورد (آن سیستم کاملاً جدا و دست‌نخورده می‌ماند).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import arbitrage_radar
import binance_client
import crypto_intelligence
import football_client
import football_dispatch
import football_fixtures
import market_pulse as market_pulse_mod
import message_templates
import promo_messages
import scheduler as scheduler_mod
import whale_alerts

DEFAULT_TICK_SECONDS = 45  # بین ۳۰ تا ۶۰ ثانیه طبق بخش ۷.۶


def _now_local(tz) -> datetime:
    return datetime.now(tz)


def _send_with_promo(context: Dict[str, Any], base_text: str, promo_state: Dict[str, Any]) -> bool:
    """متن پایه را با فوتر تبلیغاتی می‌فرستد؛ index تبلیغ فقط بعد از ارسال
    موفق commit می‌شود (بخش ۹.۴)."""
    promo = promo_messages.peek_next_promo(promo_state)
    full_text = promo_messages.append_promo_footer(base_text, promo["text"])
    success = context["send_text_message"](full_text)
    if success:
        new_state = promo_messages.commit_promo_index(promo_state, promo["index"])
        promo_state.clear()
        promo_state.update(new_state)
    return success


def dispatch_small_list(context, promo_state) -> bool:
    text = context["build_small_list_message"]()
    if not text:
        return False
    return _send_with_promo(context, text, promo_state)


def dispatch_big_list(context, promo_state) -> bool:
    text = context["build_large_list_message"]()
    if not text:
        return False
    return _send_with_promo(context, text, promo_state)


def dispatch_goodnight(context) -> bool:
    text = context.get("build_goodnight_message", lambda: None)()
    if not text:
        return False
    # طبق بخش ۸: پیام شب‌بخیر بدون دکمه و بدون تبلیغ می‌ماند (رفتار قبلی حفظ می‌شود)
    return context["send_text_message"](text, with_keyboard=False)


def dispatch_crypto_intel(context, promo_state, now: datetime) -> bool:
    result = crypto_intelligence.run_cycle(
        fetch_klines_fn=context["fetch_klines"],
        fetch_agg_trades_window_fn=context["fetch_agg_trades_window"],
        call_gemini_fn=context["call_gemini"],
        call_groq_fn=context["call_groq"],
        gemini_api_key=context.get("gemini_api_key", ""),
        groq_api_key=context.get("groq_api_key", ""),
        large_trade_threshold_usdt=context.get(
            "large_trade_threshold_usdt", binance_client.DEFAULT_LARGE_TRADE_THRESHOLD_USDT
        ),
        now=now,
    )
    if result["status"] != "sent":
        context.get("log", print)("[CENTRAL] crypto_intel skipped: {}".format(result.get("reason")))
        return False
    return _send_with_promo(context, result["message_text"], promo_state)


def dispatch_market_pulse(context, promo_state, now: datetime) -> bool:
    fng = context["fetch_fear_greed"]()
    dominance = context["fetch_btc_dominance"]()
    changes = context["fetch_24h_changes"]()
    snapshot = market_pulse_mod.build_market_pulse_snapshot(
        fng_value=fng.get("value"), fng_classification=fng.get("classification"),
        fng_timestamp_utc=fng.get("timestamp_utc"),
        btc_dominance_pct=dominance, change_24h_by_asset=changes, now=now,
    )
    if not snapshot["has_any_data"]:
        return False
    date_fa, time_fa = context["format_local_datetime"](now)
    text = message_templates.render_market_pulse_message(snapshot, date_fa, time_fa)
    return _send_with_promo(context, text, promo_state)


def dispatch_whale_alert(context, promo_state, now: datetime) -> bool:
    symbols = binance_client.ALLOWED_SYMBOLS
    whale_state = context["load_whale_state"]()
    threshold = context.get("large_trade_threshold_usdt", binance_client.DEFAULT_LARGE_TRADE_THRESHOLD_USDT)
    window_seconds = context.get("whale_scan_window_seconds", 60 * 60)
    end_ms = int(now.timestamp() * 1000)
    start_ms = end_ms - window_seconds * 1000
    best_overall = None
    best_symbol = None
    for symbol in symbols:
        try:
            trades, truncated = context["fetch_agg_trades_window"](symbol, start_ms, end_ms)
        except Exception as exc:  # noqa: BLE001
            context.get("log", print)("[CENTRAL] whale fetch failed {}: {}".format(symbol, exc))
            continue
        if truncated:
            continue
        best = binance_client.select_largest_trade(trades, threshold_usdt=threshold)
        if best and (best_overall is None or best["notional_usdt"] > best_overall["notional_usdt"]):
            best_overall, best_symbol = best, symbol

    if best_overall is None:
        return False
    reported_ids = whale_state.get(best_symbol, [])
    alert = whale_alerts.build_whale_alert(
        symbol=best_symbol, best_trade=best_overall,
        window_start_utc=datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).isoformat(),
        window_end_utc=datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).isoformat(),
        reported_ids=reported_ids, threshold_usdt=threshold,
    )
    if alert is None:
        return False
    time_local = context["format_local_time"](now)
    text = message_templates.render_whale_alert_message(alert, time_local)
    sent = _send_with_promo(context, text, promo_state)
    if sent:
        whale_state[best_symbol] = whale_alerts.remember_reported_id(reported_ids, alert["agg_trade_id"])
        context["save_whale_state"](whale_state)
    return sent


def dispatch_arbitrage(context, promo_state, now: datetime) -> bool:
    vochino_rate = context["get_vochino_usdt_sell_toman"]()
    usdt_usd, usdt_usd_at = context["get_cached_tether_usd"]()
    usd_irt, usd_irt_at = context["get_cached_usd_irt_toman"]()
    snapshot = arbitrage_radar.build_arbitrage_snapshot(
        vochino_rate_toman=vochino_rate,
        usdt_usd_rate=usdt_usd, usdt_usd_updated_at=usdt_usd_at,
        usd_irt_toman=usd_irt, usd_irt_updated_at=usd_irt_at,
        now=now,
    )
    if snapshot["status"] != "valid":
        context.get("log", print)("[CENTRAL] arbitrage skipped: {}".format(snapshot["reason"]))
        return False
    text = message_templates.render_arbitrage_message(snapshot)
    return _send_with_promo(context, text, promo_state)


_NON_FOOTBALL_DISPATCH: Dict[str, Callable] = {
    "small_list": lambda ctx, promo, now: dispatch_small_list(ctx, promo),
    "big_list": lambda ctx, promo, now: dispatch_big_list(ctx, promo),
    "crypto_intel": dispatch_crypto_intel,
    "market_pulse": dispatch_market_pulse,
    "whale_alert": dispatch_whale_alert,
    "arbitrage": dispatch_arbitrage,
    "goodnight": lambda ctx, promo, now: dispatch_goodnight(ctx),
}


def run_tick(context: Dict[str, Any], state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """یک tick کامل: پیام‌های روزمره سررسیدشده + رفرش/ارسال فوتبال. state
    شامل daily_schedule, promo, football_provider, football_fixtures,
    football_last_refresh است. خروجی: گزارش کوتاه برای لاگ/تست."""
    tz = context["tz"]
    local_date = now.astimezone(tz).date()
    report: Dict[str, Any] = {"sent": [], "failed": [], "football_sent": [], "football_actions": []}

    # ۱) زمان‌بندی روزانه هفت نوع پیام (تولید/بازیابی پایدار همان روز)
    schedule = scheduler_mod.get_or_create_today_schedule(
        state.get("daily_schedule"), local_date, tz_name=context.get("tz_name", "Asia/Tehran"),
    )
    state["daily_schedule"] = schedule

    promo_state = state.setdefault("promo", dict(promo_messages.DEFAULT_PROMO_STATE))

    for job, idx in scheduler_mod.due_jobs(schedule, now):
        handler = _NON_FOOTBALL_DISPATCH.get(job)
        if handler is None:
            continue
        try:
            ok = handler(context, promo_state, now)
        except Exception as exc:  # noqa: BLE001
            context.get("log", print)("[CENTRAL] job {} raised: {}".format(job, exc))
            ok = False
        if ok:
            scheduler_mod.mark_sent(schedule, job, idx)
            report["sent"].append(job)
        else:
            # ارسال‌نشده = دوباره در tick بعدی تلاش می‌شود (sent=False می‌ماند)؛
            # شکست یک نوع پیام بقیه پیام‌های سررسیدشده همین tick را متوقف نمی‌کند.
            report["failed"].append(job)

    # ۲) رفرش فوتبال (فقط هر N دقیقه، نه هر tick)
    poll_minutes = context.get("football_poll_interval_minutes", 15)
    last_refresh = state.get("football_last_refresh")
    needs_refresh = last_refresh is None or (now - datetime.fromisoformat(last_refresh)).total_seconds() >= poll_minutes * 60
    if needs_refresh:
        lookahead_days = context.get("football_lookahead_days", 7)
        date_from = local_date.isoformat()
        date_to_str = (local_date + timedelta(days=lookahead_days)).isoformat()
        provider_state = state.get("football_provider") or football_client.initial_provider_state()
        fixtures_state = state.get("football_fixtures") or []
        new_fixtures, new_provider_state, actions = football_dispatch.refresh_fixtures_cycle(
            provider_state=provider_state, fixtures_state=fixtures_state,
            bzzoiro_api_key=context.get("bzzoiro_api_key", ""),
            football_data_api_key=context.get("football_data_api_key", ""),
            fetch_bzzoiro_fn=context["fetch_bzzoiro_events"],
            fetch_football_data_fn=context["fetch_football_data_matches"],
            local_date=local_date, tz=tz, now=now,
            date_from=date_from, date_to=date_to_str,
        )
        state["football_fixtures"] = new_fixtures
        state["football_provider"] = new_provider_state
        state["football_last_refresh"] = now.isoformat()
        report["football_actions"] = _apply_football_actions(context, actions)

    # ۳) ارسال مسابقات سررسیدشده (دقیقاً ۹۰ دقیقه قبل از kickoff)
    fixtures_state = state.get("football_fixtures") or []
    license_confirmed = context.get("football_image_license_confirmed", False)
    for entry in football_fixtures.due_fixture_sends(fixtures_state, now):
        payload = football_dispatch.build_match_send_payload(
            entry, license_confirmed=license_confirmed, tz=tz,
            date_fa_fn=context.get("format_local_date_fn"),
            time_fa_fn=context.get("format_local_time_fn"),
        )
        promo = promo_messages.peek_next_promo(promo_state)
        caption = promo_messages.append_promo_footer(payload["caption"], promo["text"])
        msg_id = context["send_football_match"](payload["image"], caption)
        if msg_id is not None:
            new_state = promo_messages.commit_promo_index(promo_state, promo["index"])
            promo_state.clear()
            promo_state.update(new_state)
            for i, e in enumerate(fixtures_state):
                if e.get("identity_key") == entry.get("identity_key"):
                    fixtures_state[i] = football_fixtures.mark_sent(entry, now, chat_message_id=msg_id)
                    break
            report["football_sent"].append(entry.get("identity_key"))
    state["football_fixtures"] = fixtures_state

    return report


def _apply_football_actions(context: Dict[str, Any], actions: List[Dict[str, Any]]) -> List[str]:
    applied = []
    for action in actions:
        entry = action["entry"]
        msg_id = entry.get("chat_message_id")
        if msg_id is None:
            continue
        if action["type"] == "cancel":
            context["delete_football_message"](msg_id)
            applied.append("cancel:{}".format(entry.get("identity_key")))
        elif action["type"] == "edit":
            payload = football_dispatch.build_match_send_payload(
                entry, license_confirmed=context.get("football_image_license_confirmed", False),
                tz=context["tz"],
                date_fa_fn=context.get("format_local_date_fn"),
                time_fa_fn=context.get("format_local_time_fn"),
            )
            context["edit_football_message_caption"](msg_id, payload["caption"])
            applied.append("edit:{}".format(entry.get("identity_key")))
    return applied


def run_forever(context: Dict[str, Any], state: Dict[str, Any], stop_event, persist_fn: Callable[[Dict[str, Any]], None]) -> None:
    """حلقه واقعی پس‌زمینه — جایگزین poster_loop. فقط همین یک Thread برای
    همه قابلیت‌های جدید (بخش درخواست صریح کاربر برای کمترین مصرف Render)."""
    tick_seconds = context.get("tick_seconds", DEFAULT_TICK_SECONDS)
    while not stop_event.is_set():
        try:
            now = _now_local(timezone.utc)
            run_tick(context, state, now)
            persist_fn(state)
        except Exception as exc:  # noqa: BLE001
            context.get("log", print)("[CENTRAL] tick failed: {}".format(exc))
        stop_event.wait(tick_seconds)
