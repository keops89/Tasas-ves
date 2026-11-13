"""Revisa la tasa del BCV y Binance P2P una vez y avisa por webhook de Discord si hay cambios.

Pensado para ejecutarse cada pocos minutos con GitHub Actions. El último valor
avisado se guarda en estado.json para comparar en la siguiente ejecución.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from fuentes import TasaBCV, TasaBinance, obtener_bcv, obtener_binance

WEBHOOKS = [w.strip() for w in os.getenv("DISCORD_WEBHOOK", "").split(",") if w.strip()]
UMBRAL_BS = float(os.getenv("UMBRAL_BS") or 5)
LADO = (os.getenv("LADO_ALERTA") or "compra").lower()
METODOS = [m.strip() for m in (os.getenv("METODOS_PAGO") or "").split(",") if m.strip()]
ESTADO = Path(os.getenv("ARCHIVO_ESTADO", "estado.json"))


def bs(x: float) -> str:
    """1234.5 -> '1.234,50' (formato venezolano)."""
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def embed_bcv(t: TasaBCV, anterior: float | None) -> dict:
    valor = f"**{bs(t.usd)} Bs**"
    if anterior:
        cambio = (t.usd - anterior) / anterior * 100
        valor += f"\n{'🔺' if cambio >= 0 else '🔻'} {cambio:+.2f}% vs. anterior ({bs(anterior)})"
    campos = [{"name": "💵 Dólar (USD)", "value": valor, "inline": True}]
    if t.eur:
        campos.append({"name": "💶 Euro (EUR)", "value": f"**{bs(t.eur)} Bs**", "inline": True})
    campos.append({"name": "📅 Fecha valor", "value": t.fecha_texto, "inline": False})
    return {"title": "🏦 ¡Nueva tasa oficial BCV!", "url": "https://www.bcv.org.ve/",
            "color": 0x1F4E9C, "fields": campos, "timestamp": ahora()}


def embed_binance(t: TasaBinance, actual: float, ref: float) -> dict:
    sube = actual > ref
    dif = actual - ref
    return {
        "title": f"{'📈 Subió' if sube else '📉 Bajó'} el dólar en Binance P2P",
        "description": f"**{bs(ref)} → {bs(actual)} Bs** "
                       f"({'+' if sube else ''}{bs(dif)} Bs · {dif / ref * 100:+.2f}%)",
        "color": 0x2ECC71 if sube else 0xE74C3C,
        "fields": [
            {"name": "Compra USDT (pagas)", "inline": True,
             "value": f"Mejor: {bs(t.compra.mejor)} Bs\nPromedio: {bs(t.compra.promedio)} Bs"},
            {"name": "Venta USDT (recibes)", "inline": True,
             "value": f"Mejor: {bs(t.venta.mejor)} Bs\nPromedio: {bs(t.venta.promedio)} Bs"},
        ],
        "footer": {"text": f"Aviso cada ±{bs(UMBRAL_BS)} Bs · referencia: promedio de {LADO}"},
        "timestamp": ahora(),
    }


async def enviar(session: aiohttp.ClientSession, embed: dict) -> None:
    for url in WEBHOOKS:
        async with session.post(url, json={"username": "Tasas VES", "embeds": [embed]}) as r:
            if r.status >= 300:
                print(f"Error al enviar a Discord ({r.status}): {await r.text()}", file=sys.stderr)


async def main() -> int:
    if not WEBHOOKS:
        print("Falta el secreto DISCORD_WEBHOOK (ver README).", file=sys.stderr)
        return 1
    try:
        estado = json.loads(ESTADO.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        estado = {}

    async with aiohttp.ClientSession() as s:
        # --- BCV: avisar cuando cambia la fecha valor o la tasa ---
        try:
            t = await obtener_bcv(s)
            prev = estado.get("bcv")
            if prev is None:
                print(f"BCV: primera lectura {t.usd} ({t.fecha_valor}), se guarda sin avisar")
            elif prev["fecha_valor"] != t.fecha_valor or prev["usd"] != t.usd:
                await enviar(s, embed_bcv(t, prev["usd"]))
                print(f"BCV: nueva tasa {t.usd} ({t.fecha_valor}) -> avisado")
            else:
                print(f"BCV: sin cambios ({t.usd}, {t.fecha_valor})")
            estado["bcv"] = {"usd": t.usd, "eur": t.eur, "fecha_valor": t.fecha_valor}
        except Exception as ex:  # el sitio del BCV se cae con frecuencia; se reintenta la próxima vez
            print(f"BCV no disponible: {ex!r}")

        # --- Binance P2P: avisar cada vez que se mueve UMBRAL_BS desde el último aviso ---
        try:
            t = await obtener_binance(s, METODOS)
            actual = round((t.venta if LADO == "venta" else t.compra).promedio, 2)
            ref = estado.get("binance_ref")
            if ref is None:
                print(f"Binance: referencia inicial {actual}")
                estado["binance_ref"] = actual
            elif abs(actual - ref) >= UMBRAL_BS:
                await enviar(s, embed_binance(t, actual, ref))
                print(f"Binance: {ref} -> {actual} -> avisado")
                estado["binance_ref"] = actual
            else:
                print(f"Binance: {actual} (referencia {ref}, sin aviso)")
        except Exception as ex:
            print(f"Binance no disponible: {ex!r}")

    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
