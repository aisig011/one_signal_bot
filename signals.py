"""
signals.py
Логика поиска точек входа.

v5: трендовые входы считаются через risk_manager.calculate_trend_trade
(стоп от ATR, тейк на N риска вперёд) — старая схема с тейком на потолке
коридора делала R/R хуже 1:2 всегда, и трендовые сигналы не проходили.

v4: тренд по 1h определяет направление для трендовой логики,
фаза рынка (market_phase) выбирает стратегию:
- CHAOS → не торгуем
- RANGE → отбой от границ диапазона (find_range_signal)
- TREND_UP / TREND_DOWN → трендовая логика (пуллбэк/RSI/MACD)
"""

import pandas as pd

import market
import indicators
import risk_manager
import market_phase


# --- Анти-нож (риск ножа): порог наклона BTC для блокировки отбоев ---
# Отбой ПРОТИВ движения BTC часто не отбивается, а пробивает границу → стоп.
# Блокируем LONG-отбой, если BTC клонится вниз сильнее этого порога,
# и SHORT-отбой, если BTC клонится вверх сильнее порога.
# 0.3% ловит даже вялый снос BTC (тот, что общий btc_bias считает боковиком).
# Режет слишком много сигналов → подними до 0.5.
# Пропускает ножи → опусти до 0.2.
ANTI_KNIFE_BTC_SLOPE = 0.3


def find_range_signal(coin: str, symbol: str, df_1h: pd.DataFrame,
                      deposit: float, risk_percent: float,
                      min_rr: float = 2.0) -> dict | None:
    """
    Стратегия отбоя от границ диапазона (фаза RANGE / боковик).

    1. Границы диапазона = min/max за 48 свечей 1h (2 дня).
    2. Ширина диапазона >= 3% (иначе пила).
    3. Цена в зоне отбоя от границы (0.3–2%):
       - у нижней → LONG, у верхней → SHORT
       - ближе 0.3% = почти на границе/пробой (опасно), дальше 2% = не дошла
    4. RSI в рабочей зоне 30–70, объём >= 0.7.
    5. Анти-нож: не отдаём отбой ПРОТИВ движения BTC (риск ножа).
    6. SL за границей, TP к противоположной границе (или к середине).
       Минимальный SL и обрезка R/R — в risk_manager.calculate_trade.
    """
    import logging
    logger = logging.getLogger("signals")

    last = df_1h.iloc[-1]
    prev = df_1h.iloc[-2]
    close = last["close"]

    lookback = 48
    window = df_1h.iloc[-lookback:]
    range_high = window["high"].max()
    range_low = window["low"].min()
    range_width_pct = (range_high - range_low) / range_low * 100

    logger.info(
        f"diag {coin} [RANGE]: close={close:.4f}, "
        f"range_low={range_low:.4f}, range_high={range_high:.4f}, "
        f"width={range_width_pct:.2f}%"
    )

    if range_width_pct < 3.0:
        logger.info(f"diag {coin} [RANGE]: пропуск — диапазон слишком узкий ({range_width_pct:.2f}%)")
        return None

    rsi = last["rsi"]
    if rsi < 30 or rsi > 70:
        logger.info(f"diag {coin} [RANGE]: пропуск — RSI вне рабочей зоны ({rsi:.1f})")
        return None

    volume_ratio = None
    if "volume_avg" in df_1h.columns and not pd.isna(prev.get("volume_avg")):
        volume_ratio = prev["volume"] / prev["volume_avg"] if prev["volume_avg"] > 0 else 0
        logger.info(f"diag {coin} [RANGE]: volume_ratio={volume_ratio:.2f} (закрытая свеча)")
        if volume_ratio < 0.7:
            logger.info(f"diag {coin} [RANGE]: пропуск — низкий объём")
            return None

    near_low_pct = (close - range_low) / range_low * 100
    near_high_pct = (range_high - close) / range_high * 100

    direction = None
    entry_reason = None

    # Зона отбоя 0.3–2%: не на самой границе (ложный пробой) и не далеко от неё
    if 0.3 <= near_low_pct <= 2.0:
        direction = "LONG"
        entry_reason = f"отбой от нижней границы диапазона ({range_low:.4f})"
    elif 0.3 <= near_high_pct <= 2.0:
        direction = "SHORT"
        entry_reason = f"отбой от верхней границы диапазона ({range_high:.4f})"
    else:
        logger.info(
            f"diag {coin} [RANGE]: пропуск — цена вне зоны отбоя "
            f"(+{near_low_pct:.1f}% от дна, -{near_high_pct:.1f}% от верха)"
        )
        return None

    # --- Анти-нож (риск ножа): не ловим падающий/растущий нож в отбоях ---
    # Отбой от границы — самый рискованный вход: если рынок (BTC) идёт
    # против отбоя, цена не оттолкнётся, а проткнёт границу, и стоп снесёт.
    # Это ловит даже ВЯЛЫЙ снос BTC, который общий btc_bias в find_signal
    # считает боковиком (там нужно 2 признака, тут хватает наклона EMA20).
    #   LONG-отбой от нижней границы — только если BTC НЕ падает.
    #   SHORT-отбой от верхней границы — только если BTC НЕ растёт.
    btc_slope = _btc_ema20_slope()
    if direction == "LONG" and btc_slope < -ANTI_KNIFE_BTC_SLOPE:
        logger.info(
            f"diag {coin} [RANGE]: LONG-отбой отменён — риск ножа "
            f"(BTC клонится вниз, наклон EMA20 {btc_slope:.2f}%)"
        )
        return None
    if direction == "SHORT" and btc_slope > ANTI_KNIFE_BTC_SLOPE:
        logger.info(
            f"diag {coin} [RANGE]: SHORT-отбой отменён — риск ножа "
            f"(BTC клонится вверх, наклон EMA20 {btc_slope:.2f}%)"
        )
        return None

    range_mid = (range_high + range_low) / 2

    if direction == "LONG":
        trade = risk_manager.calculate_trade(
            direction=direction, entry_price=close,
            support=range_low, resistance=range_high,
            deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
        )
        tp_target = "верхняя граница диапазона"
        if trade is None:
            trade = risk_manager.calculate_trade(
                direction=direction, entry_price=close,
                support=range_low, resistance=range_mid,
                deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
            )
            tp_target = "середина диапазона"
    else:  # SHORT
        trade = risk_manager.calculate_trade(
            direction=direction, entry_price=close,
            support=range_low, resistance=range_high,
            deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
        )
        tp_target = "нижняя граница диапазона"
        if trade is None:
            trade = risk_manager.calculate_trade(
                direction=direction, entry_price=close,
                support=range_mid, resistance=range_high,
                deposit=deposit, risk_percent=risk_percent, min_rr=min_rr,
            )
            tp_target = "середина диапазона"

    if trade is None:
        logger.info(f"diag {coin} [RANGE]: пропуск — risk_manager вернул None (R/R недостаточен)")
        return None

    logger.info(
        f"diag {coin} [RANGE]: сигнал {direction}, "
        f"sl={trade['stop_loss']:.4f}, tp1={trade['take_profit_1']:.4f} ({tp_target}), "
        f"rr={trade['risk_reward']:.2f}"
    )

    return {
        "coin": coin,
        "symbol": symbol,
        "trend_1h": "range",
        "trend_4h": None,
        "rsi_1h": rsi,
        "macd_signal_1h": "bullish" if last["macd_diff"] > 0 else "bearish",
        "entry_reason": entry_reason,
        "market_phase": "RANGE",
        "volume_ratio": volume_ratio,
        "range_info": {
            "low": range_low,
            "high": range_high,
            "mid": range_mid,
            "width_pct": range_width_pct,
            "tp_target": tp_target,
        },
        "trade": trade,
    }


def _is_choppy_market() -> bool:
    """
    Определяет "дёрганый" (хаотичный, безтрендовый) рынок по BTC.

    Идея: если направление BTC за последние часы металось вверх-вниз —
    рынок неясный, и торговать в нём опасно (сигналы против движения
    ловят стопы). Как трейдер, который говорит "картина мутная, жду".

    Смотрим наклон EMA20 на нескольких отрезках (свежий, средний, дальний).
    Если знаки наклонов расходятся (был плюс, стал минус или наоборот) —
    это виляние = choppy. Также choppy, если недавняя волатильность высокая
    относительно чистого хода цены (цена много двигалась, но никуда не пришла).

    Возвращает True если рынок дёрганый (не торгуем).
    """
    import logging
    logger = logging.getLogger("signals")
    try:
        btc_symbol = market.get_symbol("BTC")
        df = market.get_klines(btc_symbol, "1h", limit=250)
        df = indicators.add_indicators(df)

        # Наклоны EMA20 на трёх отрезках: последние 6ч, 6-12ч назад, 12-18ч назад
        def slope(a_idx, b_idx):
            a = df.iloc[a_idx]["ema_20"]
            b = df.iloc[b_idx]["ema_20"]
            return (a - b) / b * 100 if b > 0 else 0

        s_recent = slope(-1, -6)    # последние 6 часов
        s_mid = slope(-6, -12)      # 6-12 часов назад
        s_far = slope(-12, -18)     # 12-18 часов назад

        # Считаем сколько раз менялся знак наклона (виляние)
        signs = []
        for s in (s_far, s_mid, s_recent):
            if s > 0.15:
                signs.append(1)
            elif s < -0.15:
                signs.append(-1)
            else:
                signs.append(0)

        # Смены направления: было +, стало -, или наоборот
        flips = 0
        prev = None
        for sg in signs:
            if sg != 0:
                if prev is not None and sg != prev:
                    flips += 1
                prev = sg

        # Чистый ход vs пройденный путь за 18 свечей (efficiency)
        window = df.iloc[-18:]
        net_move = abs(window.iloc[-1]["close"] - window.iloc[0]["close"])
        path = window["high"].max() - window["low"].min()
        efficiency = net_move / path if path > 0 else 0

        # Choppy ТОЛЬКО если рынок реально мечется:
        # - flips >= 2 (направление металось туда-сюда несколько раз), ИЛИ
        # - efficiency < 0.4 (цена много ходила, но никуда не пришла = пила)
        # ВАЖНО: flips == 1 при высокой efficiency — это НЕ хаос, а плавный
        # разворот тренда (вверх→вниз), и это хорошая точка для входа по
        # новому направлению. Такое НЕ блокируем.
        is_choppy = flips >= 2 or efficiency < 0.4

        logger.info(
            f"_is_choppy_market: slopes far={s_far:.2f} mid={s_mid:.2f} recent={s_recent:.2f}, "
            f"flips={flips}, efficiency={efficiency:.2f} → choppy={is_choppy}"
        )
        return is_choppy

    except Exception as e:
        logger.warning(f"_is_choppy_market: ошибка ({e}), считаю рынок не-choppy")
        return False  # при ошибке не блокируем (лучше пропустить чем зависнуть)


def _btc_ema20_slope() -> float:
    """
    Наклон EMA20 BTC за последние 10 свечей 1h, в процентах.

    >0 → BTC клонится вверх, <0 → вниз, ~0 → плоско.
    Используется анти-нож фильтром (риск ножа) в RANGE-отбоях: ловит даже
    вялый снос BTC, который общий _get_btc_bias ещё считает боковиком.
    При ошибке возвращает 0.0 — тогда фильтр не блокирует (лучше не зависнуть).
    """
    import logging
    logger = logging.getLogger("signals")
    try:
        btc_symbol = market.get_symbol("BTC")
        df_btc = market.get_klines(btc_symbol, "1h", limit=250)
        df_btc = indicators.add_indicators(df_btc)

        ema20 = df_btc.iloc[-1]["ema_20"]
        ema20_prev = df_btc.iloc[-10]["ema_20"] if len(df_btc) >= 10 else ema20
        slope = (ema20 - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0.0

        logger.info(f"_btc_ema20_slope: наклон EMA20 BTC = {slope:.2f}%")
        return slope

    except Exception as e:
        logger.warning(f"_btc_ema20_slope: ошибка ({e}), возвращаю 0")
        return 0.0


def _get_btc_bias() -> str | None:
    """
    Определяет "смещение" рынка по BTC (BTC тянет за собой альты).

    Использует НЕ market_phase (её порог 1.5% слишком строгий — реальный
    рост BTC часто идёт медленнее, ~1%, и market_phase считает это боковиком).
    Здесь более чувствительная оценка по нескольким признакам сразу:
    наклон EMA20, положение цены относительно EMA50, тренд по EMA50, RSI.

    Возвращает:
    - "UP"   → рынок склонен вверх (не шортим альты против роста)
    - "DOWN" → рынок склонен вниз (не лонгуем альты против падения)
    - None   → BTC реально в боковике — фильтр не применяем

    Логика: считаем "очки" за бычьи и медвежьи признаки. Нужно набрать
    хотя бы 2 признака в одну сторону, иначе считаем боковиком (None).
    """
    import logging
    logger = logging.getLogger("signals")
    try:
        btc_symbol = market.get_symbol("BTC")
        df_btc = market.get_klines(btc_symbol, "1h", limit=250)
        df_btc = indicators.add_indicators(df_btc)

        last = df_btc.iloc[-1]
        price = last["close"]
        ema20 = last["ema_20"]
        ema50 = last["ema_50"]
        rsi = last["rsi"]

        # Наклон EMA20 за 10 свечей
        ema20_prev = df_btc.iloc[-10]["ema_20"] if len(df_btc) >= 10 else ema20
        ema20_slope = (ema20 - ema20_prev) / ema20_prev * 100 if ema20_prev > 0 else 0

        bull_score = 0
        bear_score = 0

        # Признак 1: наклон EMA20 (более мягкий порог 0.5%, не 1.5%)
        if ema20_slope > 0.5:
            bull_score += 1
        elif ema20_slope < -0.5:
            bear_score += 1

        # Признак 2: цена относительно EMA50
        if price > ema50:
            bull_score += 1
        elif price < ema50:
            bear_score += 1

        # Признак 3: EMA20 относительно EMA50 (структура тренда)
        if ema20 > ema50:
            bull_score += 1
        elif ema20 < ema50:
            bear_score += 1

        # Признак 4: RSI (импульс)
        if rsi > 55:
            bull_score += 1
        elif rsi < 45:
            bear_score += 1

        logger.info(
            f"_get_btc_bias: ema20_slope={ema20_slope:.2f}%, RSI={rsi:.1f}, "
            f"price{'>' if price > ema50 else '<'}EMA50 → bull={bull_score}, bear={bear_score}"
        )

        # Нужно минимум 2 признака перевеса в одну сторону
        if bull_score >= 2 and bull_score > bear_score:
            return "UP"
        if bear_score >= 2 and bear_score > bull_score:
            return "DOWN"
        return None  # нет чёткого перевеса — боковик, не вмешиваемся

    except Exception as e:
        logger.warning(f"_get_btc_bias: ошибка определения тренда BTC: {e}")
        return None  # при ошибке фильтр не блокирует торговлю


def _find_signal_raw(coin: str, deposit: float, risk_percent: float, min_rr: float = 2.0) -> dict | None:
    """
    Ищет торговый сигнал по монете.

    1. Фаза рынка (market_phase) выбирает стратегию.
    2. CHAOS → не торгуем. RANGE → find_range_signal. TREND → трендовая логика.
    3. Трендовая: тренд 1h определяет направление, вход по
       развороту RSI / пересечению MACD / пуллбэку к EMA20.
    4. R/R хуже min_rr — сигнал не возвращается.
    """
    symbol = market.get_symbol(coin)

    df_1h = market.get_klines(symbol, "1h", limit=250)
    df_1h = indicators.add_indicators(df_1h)

    import logging
    logger = logging.getLogger("signals")

    trend_1h = indicators.get_trend(df_1h)

    last = df_1h.iloc[-1]
    prev = df_1h.iloc[-2]

    ema20_dist = abs(last["close"] - last["ema_20"]) / last["ema_20"] * 100
    logger.info(
        f"diag {coin}: trend_1h={trend_1h}, rsi={last['rsi']:.1f}, "
        f"macd_diff={last['macd_diff']:.4f} (prev {prev['macd_diff']:.4f}), "
        f"ema20_dist={ema20_dist:.2f}%"
    )

    # --- Определение фазы рынка ---
    phase_info = market_phase.detect_phase(coin, df_1h)
    phase = phase_info["phase"]
    logger.info(f"diag {coin}: market_phase={phase} ({phase_info['reason']})")

    if phase == "CHAOS":
        logger.info(f"diag {coin}: пропуск — фаза CHAOS")
        return None

    # --- RANGE — стратегия отбоя от границ диапазона ---
    if phase == "RANGE":
        result = find_range_signal(coin, symbol, df_1h, deposit, risk_percent, min_rr=min_rr)
        if result is not None:
            df_4h = market.get_klines(symbol, "4h", limit=250)
            df_4h = indicators.add_indicators(df_4h)
            result["trend_4h"] = indicators.get_trend(df_4h)
        return result

    # --- Трендовая логика (TREND_UP / TREND_DOWN) ---

    if last["rsi"] < 25 or last["rsi"] > 75:
        logger.info(f"diag {coin}: пропуск — экстремальный RSI {last['rsi']:.1f}")
        return None

    if trend_1h == "flat":
        return None

    direction = None
    entry_reason = None

    # Зона пуллбэка расширена до 2.5% (было 1.2% — слишком тесно, редко срабатывало).
    # Вход "по тренду на откате": цена откатилась к EMA20 и разворачивается
    # обратно в сторону тренда — это надёжный вход ПО движению.
    ema20_distance_pct = abs(last["close"] - last["ema_20"]) / last["ema_20"] * 100
    near_ema20 = ema20_distance_pct <= 2.5

    # Свеча разворачивается обратно в сторону тренда (текущая vs предыдущая цена)
    price_turning_up = last["close"] > prev["close"]
    price_turning_down = last["close"] < prev["close"]

    if trend_1h == "bullish":
        rsi_recovering = prev["rsi"] < 40 and last["rsi"] > prev["rsi"]
        macd_cross_up = prev["macd_diff"] <= 0 and last["macd_diff"] > 0
        # Пуллбэк к EMA20 в аптренде + цена разворачивается вверх = вход по тренду
        pullback = near_ema20 and last["rsi"] < 68 and price_turning_up

        if pullback:
            direction = "LONG"
            entry_reason = "пуллбэк к EMA20 в восходящем тренде"
        elif rsi_recovering:
            direction = "LONG"
            entry_reason = "разворот RSI вверх в восходящем тренде"
        elif macd_cross_up:
            direction = "LONG"
            entry_reason = "пересечение MACD вверх по тренду"

    elif trend_1h == "bearish":
        rsi_recovering = prev["rsi"] > 60 and last["rsi"] < prev["rsi"]
        macd_cross_down = prev["macd_diff"] >= 0 and last["macd_diff"] < 0
        pullback = near_ema20 and last["rsi"] > 32 and price_turning_down

        if pullback:
            direction = "SHORT"
            entry_reason = "пуллбэк к EMA20 в нисходящем тренде"
        elif rsi_recovering:
            direction = "SHORT"
            entry_reason = "разворот RSI вниз в нисходящем тренде"
        elif macd_cross_down:
            direction = "SHORT"
            entry_reason = "пересечение MACD вниз по тренду"

    if direction is None:
        return None

    # --- Подтверждение объёмом ---
    # ВАЖНО: берём объём ПРЕДЫДУЩЕЙ ЗАКРЫТОЙ свечи (prev), а не текущей (last).
    # Текущая свеча ещё формируется (например прошло 2 мин из 60), её объём
    # почти нулевой → volume_ratio выходил 0.02-0.27 и резал все сигналы.
    # Предыдущая свеча полная — её объём корректен для сравнения.
    if "volume_avg" in df_1h.columns and not pd.isna(prev.get("volume_avg")):
        volume_ratio = prev["volume"] / prev["volume_avg"] if prev["volume_avg"] > 0 else 0
        logger.info(f"diag {coin}: volume_ratio={volume_ratio:.2f} (закрытая свеча)")
        if volume_ratio < 0.7:
            return None
    else:
        volume_ratio = None

    df_4h = market.get_klines(symbol, "4h", limit=250)
    df_4h = indicators.add_indicators(df_4h)
    trend_4h = indicators.get_trend(df_4h)

    entry_price = last["close"]

    # --- Расчёт сделки для трендового входа ---
    # ВАЖНО: используем calculate_trend_trade, а НЕ calculate_trade.
    # Старая схема (стоп под дном за 30 свечей, тейк на потолке) требовала,
    # чтобы цена была в нижней трети коридора. Вход на откате по тренду —
    # это всегда верхняя часть коридора, поэтому R/R выходил хуже 1:2 и
    # трендовые сигналы не проходили НИКОГДА. Теперь стоп от ATR (за шумом
    # монеты), тейк на TREND_RR_TARGET риска вперёд.
    trade = risk_manager.calculate_trend_trade(
        direction=direction,
        entry_price=entry_price,
        atr=last.get("atr"),
        deposit=deposit,
        risk_percent=risk_percent,
        min_rr=min_rr,
        coin=coin,
    )

    if trade is None:
        # Причина уже записана в лог внутри calculate_trend_trade
        return None

    return {
        "coin": coin,
        "symbol": symbol,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "rsi_1h": last["rsi"],
        "macd_signal_1h": "bullish" if last["macd_diff"] > 0 else "bearish",
        "entry_reason": entry_reason,
        "market_phase": phase,
        "volume_ratio": (prev["volume"] / prev["volume_avg"]) if ("volume_avg" in df_1h.columns and not pd.isna(prev.get("volume_avg")) and prev["volume_avg"] > 0) else None,
        "trade": trade,
    }



def _calc_signal_quality(result: dict) -> dict:
    """
    Считает балл качества сигнала (информативно, не блокирует).

    Очки:
    - Объём: x1.5+ → +2, x1.0-1.5 → +1, ниже → 0
    - Чистота отбоя (для RANGE, зона 0.5-1.5% от границы → +2, край → +1)
    - RSI (не на краях, больше места для движения → +2, ближе к краю → +1)
    - Согласование 4h (по направлению → +2, флэт → +1)
    - Ширина диапазона 5%+ → +1

    Возвращает {"score": int, "max": int, "label": "🔥/✅/⚠️", "reasons": [...]}
    """
    score = 0
    reasons = []

    direction = result["trade"]["direction"]
    ri = result.get("range_info") or {}

    # --- Объём ---
    vr = result.get("volume_ratio")
    if vr is not None:
        if vr >= 1.5:
            score += 2
            reasons.append(f"объём сильный (x{vr:.1f})")
        elif vr >= 1.0:
            score += 1
            reasons.append(f"объём норм (x{vr:.1f})")
        else:
            reasons.append(f"объём слабый (x{vr:.1f})")

    # --- Чистота отбоя (только для RANGE, где есть границы) ---
    close = result["trade"]["entry_price"]
    if ri:
        low = ri.get("low")
        high = ri.get("high")
        if direction == "LONG" and low:
            dist_pct = (close - low) / low * 100
        elif direction == "SHORT" and high:
            dist_pct = (high - close) / high * 100
        else:
            dist_pct = None

        if dist_pct is not None:
            if 0.5 <= dist_pct <= 1.5:
                score += 2
                reasons.append("отбой в идеальной зоне")
            else:
                score += 1
                reasons.append("отбой на краю зоны")

    # --- RSI ---
    rsi = result.get("rsi_1h")
    if rsi is not None:
        if direction == "LONG":
            if 35 <= rsi <= 55:
                score += 2
                reasons.append(f"RSI в хорошей зоне ({rsi:.0f})")
            else:
                score += 1
        else:  # SHORT
            if 45 <= rsi <= 65:
                score += 2
                reasons.append(f"RSI в хорошей зоне ({rsi:.0f})")
            else:
                score += 1

    # --- Согласование с 4h ---
    trend_4h = result.get("trend_4h")
    if trend_4h == "bullish" and direction == "LONG":
        score += 2
        reasons.append("4h подтверждает (вверх)")
    elif trend_4h == "bearish" and direction == "SHORT":
        score += 2
        reasons.append("4h подтверждает (вниз)")
    elif trend_4h in ("flat", "range", None):
        score += 1

    # --- Ширина диапазона ---
    width = ri.get("width_pct")
    if width and width >= 5.0:
        score += 1
        reasons.append(f"широкий диапазон ({width:.1f}%)")

    max_score = 9
    if score >= 7:
        label = "🔥 Сильный"
    elif score >= 5:
        label = "✅ Хороший"
    else:
        label = "⚠️ Средний"

    return {"score": score, "max": max_score, "label": label, "reasons": reasons}


def find_signal(coin: str, deposit: float, risk_percent: float, min_rr: float = 2.0) -> dict | None:
    """
    Обёртка над _find_signal_raw с фильтром по тренду BTC.

    BTC тянет за собой весь альткоин-рынок. Торговать против сильного
    движения BTC — частая причина стопов (шорт при растущем рынке,
    лонг при падающем). Поэтому:
    - BTC растёт → не отдаём SHORT (ни по альтам, ни по самому BTC)
    - BTC падает → не отдаём LONG (ни по альтам, ни по самому BTC)
    - BTC в боковике/хаосе → фильтр не применяется
    """
    import logging
    logger = logging.getLogger("signals")

    result = _find_signal_raw(coin, deposit, risk_percent, min_rr=min_rr)
    if result is None:
        return None

    # Фильтр №1: дёрганый рынок — не торгуем вообще.
    # Если BTC мечется вверх-вниз без чёткого направления, любые входы
    # (и лонги, и шорты) ловят стопы. Лучше переждать, как трейдер.
    if _is_choppy_market():
        logger.info(f"diag {coin}: сигнал отменён — рынок дёрганый (choppy), пережидаем")
        return None

    direction = result["trade"]["direction"]
    btc_bias = _get_btc_bias()

    # BTC тоже фильтруется по своему тренду: если BTC склонен вверх — не
    # шортим его (шорт против собственного роста = стоп, как у альтов).
    if btc_bias == "UP" and direction == "SHORT":
        logger.info(f"diag {coin}: SHORT отменён — BTC в восходящем тренде (не шортим против роста рынка)")
        return None

    if btc_bias == "DOWN" and direction == "LONG":
        logger.info(f"diag {coin}: LONG отменён — BTC в нисходящем тренде (не лонгуем против падения рынка)")
        return None

    # --- Мульти-ТФ подтверждение (Вариант А, мягкий) ---
    # Сигнал на 1h не должен идти против ЯВНОГО тренда на 4h (старший ТФ).
    # 4h bearish → не лонгуем (лонг против падения старшего ТФ).
    # 4h bullish → не шортим (шорт против роста старшего ТФ).
    # 4h flat/range → не мешаем, пропускаем оба направления.
    trend_4h = result.get("trend_4h")

    if trend_4h == "bearish" and direction == "LONG":
        logger.info(f"diag {coin}: LONG отменён — 4h в нисходящем тренде (вход против старшего ТФ)")
        return None

    if trend_4h == "bullish" and direction == "SHORT":
        logger.info(f"diag {coin}: SHORT отменён — 4h в восходящем тренде (вход против старшего ТФ)")
        return None

    # Сигнал прошёл все фильтры — считаем балл качества (информативно)
    result["quality"] = _calc_signal_quality(result)
    logger.info(f"diag {coin}: качество сигнала {result['quality']['label']} "
                f"({result['quality']['score']}/{result['quality']['max']})")

    return result
