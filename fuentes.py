"""Fuentes de datos: Binance P2P (USDT/VES) y Banco Central de Venezuela (BCV)."""
from __future__ import annotations

import re
import ssl
import statistics
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
import certifi
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

BINANCE_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
BCV_URL = "https://www.bcv.org.ve/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TasasVESBot/1.0)"}
TIMEOUT = aiohttp.ClientTimeout(total=30)


# --------------------------------------------------------------------------- #
# Binance P2P
# --------------------------------------------------------------------------- #
@dataclass
class LadoP2P:
    mejor: float          # mejor precio del lado
    promedio: float       # mediana de los primeros N anuncios (resiste anuncios raros)
    anuncios: int


@dataclass
class TasaBinance:
    compra: LadoP2P       # lo que pagas en Bs por 1 USDT (anuncios de venta de USDT)
    venta: LadoP2P        # lo que recibes en Bs por 1 USDT (anuncios de compra de USDT)
    hora: datetime


async def _lado_binance(session: aiohttp.ClientSession, trade_type: str,
                        pay_types: list[str], monto_bs: float | None, rows: int) -> LadoP2P:
    body = {
        "asset": "USDT", "fiat": "VES", "tradeType": trade_type,
        "page": 1, "rows": rows, "payTypes": pay_types,
        "publisherType": None, "countries": [],
    }
    if monto_bs:
        body["transAmount"] = str(monto_bs)
    async with session.post(BINANCE_URL, json=body, headers=HEADERS, timeout=TIMEOUT) as r:
        r.raise_for_status()
        data = await r.json()
    precios = [float(a["adv"]["price"]) for a in data.get("data") or []]
    if not precios:
        raise RuntimeError(f"Binance P2P no devolvió anuncios ({trade_type})")
    return LadoP2P(mejor=precios[0], promedio=statistics.median(precios), anuncios=len(precios))


async def obtener_binance(session: aiohttp.ClientSession, pay_types: list[str] | None = None,
                          monto_bs: float | None = None, rows: int = 10) -> TasaBinance:
    pay_types = pay_types or []
    compra = await _lado_binance(session, "BUY", pay_types, monto_bs, rows)
    venta = await _lado_binance(session, "SELL", pay_types, monto_bs, rows)
    return TasaBinance(compra=compra, venta=venta, hora=datetime.now())


# --------------------------------------------------------------------------- #
# BCV
# --------------------------------------------------------------------------- #
@dataclass
class TasaBCV:
    usd: float
    eur: float | None
    fecha_valor: str      # ISO (AAAA-MM-DD) de la fecha en que rige la tasa
    fecha_texto: str      # como la muestra el BCV, ej. "Lunes, 17 Noviembre 2026"


def _num_ve(texto: str) -> float:
    """'36,51540000' -> 36.5154 (formato venezolano: punto de miles, coma decimal)."""
    limpio = texto.strip().replace(".", "").replace(",", ".")
    return float(re.sub(r"[^\d.]", "", limpio))


def parsear_bcv(html: str) -> TasaBCV:
    soup = BeautifulSoup(html, "html.parser")

    def moneda(div_id: str) -> float | None:
        div = soup.find(id=div_id)
        strong = div.find("strong") if div else None
        return _num_ve(strong.get_text()) if strong else None

    usd = moneda("dolar")
    if usd is None:
        raise RuntimeError("No encontré la tasa del dólar en la página del BCV (¿cambió el diseño?)")
    fecha = soup.select_one("span.date-display-single")
    if fecha is None:
        raise RuntimeError("No encontré la 'Fecha Valor' en la página del BCV")
    iso = (fecha.get("content") or "")[:10]
    texto = " ".join(fecha.get_text().split())
    return TasaBCV(usd=usd, eur=moneda("euro"), fecha_valor=iso or texto, fecha_texto=texto)


# El servidor del BCV suele no enviar su certificado intermedio, lo que hace fallar la
# verificación SSL. En lugar de desactivar la verificación, descargamos el intermedio que
# falta desde la URL que indica el propio certificado (AIA) y verificamos contra las CA
# raíz de confianza de siempre.
_ctx_bcv: ssl.SSLContext | None = None


async def _contexto_ssl_bcv(session: aiohttp.ClientSession) -> ssl.SSLContext:
    global _ctx_bcv
    if _ctx_bcv is not None:
        return _ctx_bcv
    import asyncio
    host = urlparse(BCV_URL).hostname
    pem = await asyncio.to_thread(ssl.get_server_certificate, (host, 443))
    hoja = x509.load_pem_x509_certificate(pem.encode())
    aia = hoja.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    urls = [d.access_location.value for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
    ctx = ssl.create_default_context(cafile=certifi.where())
    for url in urls:
        async with session.get(url, timeout=TIMEOUT) as r:
            der = await r.read()
        try:
            inter = x509.load_der_x509_certificate(der)
        except ValueError:
            inter = x509.load_pem_x509_certificate(der)
        ctx.load_verify_locations(cadata=inter.public_bytes(Encoding.PEM).decode())
    _ctx_bcv = ctx
    return ctx


DOLARAPI = "https://ve.dolarapi.com/v1/{}/oficial"
_DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
          "Septiembre", "Octubre", "Noviembre", "Diciembre"]


async def obtener_bcv_respaldo(session: aiohttp.ClientSession) -> TasaBCV:
    """Respaldo: DolarApi replica la tasa oficial publicada por el BCV."""
    async with session.get(DOLARAPI.format("dolares"), headers=HEADERS, timeout=TIMEOUT) as r:
        r.raise_for_status()
        usd = await r.json()
    eur = None
    try:
        async with session.get(DOLARAPI.format("euros"), headers=HEADERS, timeout=TIMEOUT) as r:
            r.raise_for_status()
            eur = float((await r.json())["promedio"])
    except Exception:
        pass
    iso = usd["fechaActualizacion"][:10]
    f = datetime.strptime(iso, "%Y-%m-%d")
    texto = f"{_DIAS[f.weekday()]}, {f.day:02d} {_MESES[f.month - 1]} {f.year}"
    return TasaBCV(usd=float(usd["promedio"]), eur=eur, fecha_valor=iso, fecha_texto=texto)


async def obtener_bcv(session: aiohttp.ClientSession) -> TasaBCV:
    """Lee la web del BCV; si no responde (se cae a menudo o bloquea servidores extranjeros), usa DolarApi."""
    try:
        return await _obtener_bcv_web(session)
    except Exception as ex:
        print(f"Web del BCV no disponible ({type(ex).__name__}); usando respaldo DolarApi")
        return await obtener_bcv_respaldo(session)


async def _obtener_bcv_web(session: aiohttp.ClientSession) -> TasaBCV:
    try:
        async with session.get(BCV_URL, headers=HEADERS, timeout=TIMEOUT) as r:
            r.raise_for_status()
            html = await r.text()
    except aiohttp.ClientConnectorCertificateError:
        ctx = await _contexto_ssl_bcv(session)
        async with session.get(BCV_URL, headers=HEADERS, timeout=TIMEOUT, ssl=ctx) as r:
            r.raise_for_status()
            html = await r.text()
    return parsear_bcv(html)
