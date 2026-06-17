"""Цены моделей (₽ за 1 000 000 токенов) — отдельно ВЫХОД (наш запрос/prompt) и
ВХОД (ответ сервера/completion). routerai цену в API не отдаёт, поэтому держим таблицу здесь.

Терминология владельца:
- ВЫХОД = запрос ОТ нас на сервер  = prompt_tokens   (input в терминах API)
- ВХОД  = получение данных С сервера = completion_tokens (output в терминах API)

ВАЖНО: цены — РЕДАКТИРУЕМЫЕ заглушки. Поставь реальные ₽/1М с routerai (скажи — впишу точные).
"""
from __future__ import annotations

# model_id → (₽ за 1М ВЫХОД/prompt, ₽ за 1М ВХОД/completion)
PRICES_RUB_PER_1M: dict[str, tuple[float, float]] = {
    # (prompt ₽/1М, completion ₽/1М) — routerai «входящие»=prompt, «исходящие»=completion
    "qwen/qwen3-next-80b-a3b-instruct": (9.0, 73.0),
    "qwen/qwen3-vl-30b-a3b-instruct":   (12.0, 48.0),
    "qwen/qwen3-vl-32b-instruct":       (40.0, 160.0),
    "deepseek/deepseek-v4-flash":       (20.0, 80.0),
    "qwen/qwen3-235b-a22b-2507":        (60.0, 240.0),
}

# фолбэк, если модель не в таблице
_DEFAULT = (30.0, 120.0)


def _rules_from_settings() -> dict[str, tuple[float, float]]:
    """Цены из настройки AI_PRICE_RULES_JSON (редактируется в UI) — приоритетный источник."""
    out: dict[str, tuple[float, float]] = {}
    try:
        import json
        from .config import settings
        raw = getattr(settings, "ai_price_rules_json", "") or ""
        if not raw.strip():
            return out
        data = json.loads(raw)
        rows = data if isinstance(data, list) else (data.get("models") or [])
        for r in rows:
            m = str(r.get("model") or r.get("id") or "").strip()
            if not m:
                continue
            inp = float(r.get("input_per_mtok_rub") or 0)   # prompt = ВЫХОД
            outp = float(r.get("output_per_mtok_rub") or 0)  # completion = ВХОД
            out[m] = (inp, outp)
    except Exception:
        pass
    return out


def price_for(model: str | None) -> tuple[float, float]:
    if not model:
        return _DEFAULT
    # 1) настройка из UI (один источник правды), 2) встроенная таблица, 3) дефолт
    table = {**PRICES_RUB_PER_1M, **_rules_from_settings()}
    if model in table:
        return table[model]
    for k, v in table.items():
        if k.split("/")[-1] in model or model.split("/")[-1] in k:
            return v
    return _DEFAULT


def cost_rub(model: str | None, prompt_tokens: int, completion_tokens: int) -> dict[str, float]:
    """₽ за конкретный вызов: вых (prompt), вх (completion), итого."""
    p_out, p_in = price_for(model)
    out_rub = (int(prompt_tokens or 0) / 1_000_000) * p_out      # ВЫХОД = наш запрос
    in_rub = (int(completion_tokens or 0) / 1_000_000) * p_in    # ВХОД = ответ сервера
    return {"out_rub": round(out_rub, 4), "in_rub": round(in_rub, 4), "total_rub": round(out_rub + in_rub, 4)}
