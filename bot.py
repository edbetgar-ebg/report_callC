#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de Telegram: Registro + Reporte de Ventas/Horas + Informes
Requisitos:
    pip install "python-telegram-bot>=20"

Token:
    - En local: edita TOKEN_DEFAULT abajo con tu token.
    - En Render: agrega la Environment Variable TOKEN con tu token.
"""

import calendar
import logging
import os
import sqlite3
import threading
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ─────────────────────── CONFIGURACIÓN ───────────────────────
TOKEN_DEFAULT = "8681409894:AAFNNSfSJVEIGSBsPXBIQ30OWpcH6ciwxSQ"
TOKEN         = os.environ.get("TOKEN", TOKEN_DEFAULT)

DB_PATH       = os.environ.get("DB_PATH", "bot.db")
ANIO_MINIMO   = 2026
META_VENTAS   = 90
PAGO_POR_HORA = 1

MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
         "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

ROLES = ("TExp", "PV", "Ret")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
log = logging.getLogger(__name__)

# ─────────────────────── BASE DE DATOS ───────────────────────
def _con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = _con()
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        role       TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS reports(
        user_id INTEGER NOT NULL,
        day     TEXT NOT NULL,
        hours   INTEGER NOT NULL,
        sales   INTEGER NOT NULL,
        PRIMARY KEY (user_id, day))""")
    con.commit()
    con.close()

def get_user(uid):
    con = _con()
    r = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r

def add_user(uid, username, role):
    con = _con()
    con.execute("INSERT INTO users(user_id,username,role,created_at) VALUES(?,?,?,?)",
                (uid, username, role, date.today().isoformat()))
    con.commit()
    con.close()

def delete_user(uid):
    con = _con()
    con.execute("DELETE FROM reports WHERE user_id=?", (uid,))
    con.execute("DELETE FROM users   WHERE user_id=?", (uid,))
    con.commit()
    con.close()

def upsert_report(uid, day, hours, sales):
    con = _con()
    con.execute("""INSERT INTO reports(user_id,day,hours,sales) VALUES(?,?,?,?)
                   ON CONFLICT(user_id,day) DO UPDATE SET
                       hours = excluded.hours,
                       sales = excluded.sales""", (uid, day, hours, sales))
    con.commit()
    con.close()

def get_reports(uid, start, end):
    con = _con()
    rows = con.execute("""SELECT day,hours,sales FROM reports
                          WHERE user_id=? AND day BETWEEN ? AND ?
                          ORDER BY day""", (uid, start, end)).fetchall()
    con.close()
    return rows

# ─────────────────────── TECLADOS ───────────────────────
MENU = ReplyKeyboardMarkup(
    [["📝 Registrarse", "📊 Reportar"],
     ["📈 Informe",     "🗑 Eliminar datos"]],
    resize_keyboard=True)

CANCEL = ReplyKeyboardMarkup([["❌ Cancelar"]], resize_keyboard=True)

MENU_FILTER = filters.Regex(r"^(📝 Registrarse|📊 Reportar|📈 Informe|🗑 Eliminar datos|❌ Cancelar)$")
INPUT       = filters.TEXT & ~filters.COMMAND & ~MENU_FILTER

# ─────────────────────── ESTADOS ───────────────────────
(S_REG_ROLE, S_DEL_CONFIRM,
 S_REP_YEAR, S_REP_MONTH, S_REP_DAY, S_REP_HOURS, S_REP_SALES,
 S_INF_CHOICE, S_MES_YEAR, S_MES_MONTH,
 S_TOT_YEAR, S_TOT_RANGE) = range(12)

# ─────────────────────── UTILIDADES ───────────────────────
def valid_date(y, m, d):
    if y < ANIO_MINIMO or not 1 <= m <= 12:
        return False
    return 1 <= d <= calendar.monthrange(y, m)[1]

def build_monthly_report(uid, year, month):
    last = calendar.monthrange(year, month)[1]
    rows = get_reports(uid, f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last:02d}")

    q1 = [r for r in rows if int(r["day"][8:10]) <= 15]
    q2 = [r for r in rows if int(r["day"][8:10]) > 15]

    v1 = sum(r["sales"] for r in q1); h1 = sum(r["hours"] for r in q1)
    v2 = sum(r["sales"] for r in q2); h2 = sum(r["hours"] for r in q2)

    lineas = [
        f"📅 Mes: {MESES[month]} {year}",
        "",
        "1ra Quincena",
        f"  Ventas: {v1}",
        f"  Horas: {h1}",
        "",
        "2da Quincena",
        f"  Ventas: {v2}",
        f"  Horas: {h2}",
    ]

    extra = _premio(year, month, v1 + v2, {r["day"] for r in rows})
    if extra:
        lineas += ["", extra]
    return "\n".join(lineas)

def _premio(year, month, ventas_mes, dias_reportados):
    hoy = date.today()
    if (year, month) != (hoy.year, hoy.month):
        return None

    last = calendar.monthrange(year, month)[1]
    pendientes = 0
    for d in range(hoy.day, last + 1):
        dd = date(year, month, d)
        if dd.weekday() < 5 and dd.isoformat() not in dias_reportados:
            pendientes += 1

    if pendientes == 0:
        return None

    falta = META_VENTAS - ventas_mes
    if falta <= 0:
        return "🎉 ¡Meta de ventas del mes ya alcanzada!"
    return (f"🎯 Ventas diarias necesarias para premio: {falta / pendientes:.2f}  "
            f"({falta} ventas en {pendientes} días laborables restantes)")

def build_general_report(uid, year, m1, m2):
    last = calendar.monthrange(year, m2)[1]
    rows = get_reports(uid, f"{year}-{m1:02d}-01", f"{year}-{m2:02d}-{last:02d}")
    ventas = sum(r["sales"] for r in rows)
    horas  = sum(r["hours"] for r in rows)
    return "\n".join([
        f"📅 Meses: {MESES[m1]} a {MESES[m2]} {year}",
        f"Ventas: {ventas}",
        f"Horas: {horas}",
        f"Salario recaudado: {horas * PAGO_POR_HORA} USDT",
    ])

# ─────────────────────── /start y /cancel ───────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hola, soy tu bot de reportes.\nUsa el menú inferior:",
        reply_markup=MENU)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("rep", None)
    await update.message.reply_text("❌ Operación cancelada.", reply_markup=MENU)
    return ConversationHandler.END

# ─────────────────────── REGISTRO ───────────────────────
async def start_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    if u:
        await update.message.reply_text(
            f"ℹ️ Ya estás registrado con el rol *{u['role']}*.\n"
            "Si deseas cambiar de rol, elimina tus datos primero.",
            parse_mode="Markdown", reply_markup=MENU)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("TExp", callback_data="role:TExp"),
        InlineKeyboardButton("PV",   callback_data="role:PV"),
        InlineKeyboardButton("Ret",  callback_data="role:Ret"),
    ]])
    await update.message.reply_text("Selecciona tu rol:", reply_markup=kb)
    return S_REG_ROLE

async def role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if get_user(uid):
        await q.edit_message_text("ℹ️ Ya estás registrado.")
        return ConversationHandler.END

    role = q.data.split(":", 1)[1]
    if role not in ROLES:
        await q.edit_message_text("❌ Rol inválido.")
        return ConversationHandler.END

    add_user(uid, q.from_user.username, role)
    await q.edit_message_text(f"✅ Registro exitoso.\nRol: *{role}*", parse_mode="Markdown")
    await context.bot.send_message(uid, "Usa el menú para continuar.", reply_markup=MENU)
    return ConversationHandler.END

# ─────────────────────── ELIMINAR DATOS ───────────────────────
async def start_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(uid):
        await update.message.reply_text("⚠️ No estás registrado.", reply_markup=MENU)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí, eliminar todo", callback_data="del:yes"),
        InlineKeyboardButton("❌ Cancelar",          callback_data="del:no"),
    ]])
    await update.message.reply_text(
        "⚠️ ¿Seguro que quieres eliminar TODOS tus datos?\n"
        "Esta acción no se puede deshacer.",
        reply_markup=kb)
    return S_DEL_CONFIRM

async def delete_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "del:yes":
        delete_user(q.from_user.id)
        await q.edit_message_text("🗑 Todos tus datos fueron eliminados.")
    else:
        await q.edit_message_text("Operación cancelada.")
    await context.bot.send_message(q.from_user.id, "Menú principal:", reply_markup=MENU)
    return ConversationHandler.END

# ─────────────────────── REPORTAR ───────────────────────
async def start_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(uid):
        await update.message.reply_text("⚠️ Primero debes registrarte.", reply_markup=MENU)
        return ConversationHandler.END
    context.user_data["rep"] = {}
    await update.message.reply_text(
        f"📅 Introduce el AÑO (número ≥ {ANIO_MINIMO}):", reply_markup=CANCEL)
    return S_REP_YEAR

async def rep_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        y = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Debe ser un número entero. Año:")
        return S_REP_YEAR
    if y < ANIO_MINIMO:
        await update.message.reply_text(f"❌ El año debe ser ≥ {ANIO_MINIMO}:")
        return S_REP_YEAR
    context.user_data["rep"]["year"] = y
    await update.message.reply_text("📅 Ahora el MES (1-12):", reply_markup=CANCEL)
    return S_REP_MONTH

async def rep_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero entre 1 y 12:")
        return S_REP_MONTH
    if not 1 <= m <= 12:
        await update.message.reply_text("❌ Mes inválido (1-12):")
        return S_REP_MONTH
    context.user_data["rep"]["month"] = m
    await update.message.reply_text("📆 Ahora el DÍA (1-31):", reply_markup=CANCEL)
    return S_REP_DAY

async def rep_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["rep"]
    try:
        day = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Día inválido:")
        return S_REP_DAY
    if not valid_date(d["year"], d["month"], day):
        maxd = calendar.monthrange(d["year"], d["month"])[1]
        await update.message.reply_text(f"❌ Fecha inválida. Día entre 1 y {maxd}:")
        return S_REP_DAY
    d["day"] = day
    await update.message.reply_text("⏱ Horas trabajadas (entero 0-15):", reply_markup=CANCEL)
    return S_REP_HOURS

async def rep_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        h = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero entre 0 y 15:")
        return S_REP_HOURS
    if not 0 <= h <= 15:
        await update.message.reply_text("❌ Las horas deben estar entre 0 y 15:")
        return S_REP_HOURS
    context.user_data["rep"]["hours"] = h
    await update.message.reply_text("🛒 Ventas (entero 0-15):", reply_markup=CANCEL)
    return S_REP_SALES

async def rep_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero entre 0 y 15:")
        return S_REP_SALES
    if not 0 <= s <= 15:
        await update.message.reply_text("❌ Las ventas deben estar entre 0 y 15:")
        return S_REP_SALES

    d = context.user_data["rep"]
    uid = update.effective_user.id
    dia = f"{d['year']}-{d['month']:02d}-{d['day']:02d}"

    existia = bool(get_reports(uid, dia, dia))
    upsert_report(uid, dia, d["hours"], s)

    estado = "🔄 Datos sobrescritos" if existia else "✅ Reporte guardado"
    await update.message.reply_text(
        f"{estado}\n\n"
        f"📅 Fecha: {d['day']:02d}/{d['month']:02d}/{d['year']}\n"
        f"⏱ Horas: {d['hours']}\n"
        f"🛒 Ventas: {s}",
        reply_markup=MENU)
    context.user_data.pop("rep", None)
    return ConversationHandler.END

# ─────────────────────── INFORME ───────────────────────
async def start_informe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(uid):
        await update.message.reply_text("⚠️ Primero debes registrarte.", reply_markup=MENU)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Mes",   callback_data="inf:mes"),
        InlineKeyboardButton("📊 Total", callback_data="inf:total"),
    ]])
    await update.message.reply_text("¿Qué informe deseas?", reply_markup=kb)
    return S_INF_CHOICE

async def informe_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if q.data == "inf:mes":
        await context.bot.send_message(q.from_user.id,
            f"📅 Introduce el AÑO (≥ {ANIO_MINIMO}):", reply_markup=CANCEL)
        return S_MES_YEAR
    else:
        await context.bot.send_message(q.from_user.id,
            f"📅 Introduce el AÑO (≥ {ANIO_MINIMO}):", reply_markup=CANCEL)
        return S_TOT_YEAR

async def mes_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        y = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero. Año:")
        return S_MES_YEAR
    if y < ANIO_MINIMO:
        await update.message.reply_text(f"❌ Año ≥ {ANIO_MINIMO}:")
        return S_MES_YEAR
    context.user_data["mes_year"] = y
    await update.message.reply_text("📅 Número del MES (1-12):", reply_markup=CANCEL)
    return S_MES_MONTH

async def mes_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero entre 1 y 12:")
        return S_MES_MONTH
    if not 1 <= m <= 12:
        await update.message.reply_text("❌ Mes inválido (1-12):")
        return S_MES_MONTH

    y = context.user_data.pop("mes_year")
    texto = build_monthly_report(update.effective_user.id, y, m)
    await update.message.reply_text(texto, reply_markup=MENU)
    return ConversationHandler.END

async def tot_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        y = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Número entero. Año:")
        return S_TOT_YEAR
    if y < ANIO_MINIMO:
        await update.message.reply_text(f"❌ Año ≥ {ANIO_MINIMO}:")
        return S_TOT_YEAR
    context.user_data["tot_year"] = y
    await update.message.reply_text(
        "📅 Mes inicio y mes final separados por un espacio (ej: 3 8):",
        reply_markup=CANCEL)
    return S_TOT_RANGE

async def tot_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    partes = update.message.text.strip().split()
    if len(partes) != 2:
        await update.message.reply_text("❌ Formato: mes_inicio mes_fin (ej: 3 8):")
        return S_TOT_RANGE
    try:
        m1, m2 = int(partes[0]), int(partes[1])
    except ValueError:
        await update.message.reply_text("❌ Deben ser números. Ej: 3 8:")
        return S_TOT_RANGE
    if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
        await update.message.reply_text("❌ Los meses deben estar entre 1 y 12:")
        return S_TOT_RANGE
    if m1 > m2:
        m1, m2 = m2, m1

    y = context.user_data.pop("tot_year")
    texto = build_general_report(update.effective_user.id, y, m1, m2)
    await update.message.reply_text(texto, reply_markup=MENU)
    return ConversationHandler.END

# ─────────────────────── CONVERSACIÓN ───────────────────────
conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("start", cmd_start),
        MessageHandler(filters.Regex(r"^📝 Registrarse$"),    start_register),
        MessageHandler(filters.Regex(r"^📊 Reportar$"),       start_report),
        MessageHandler(filters.Regex(r"^📈 Informe$"),        start_informe),
        MessageHandler(filters.Regex(r"^🗑 Eliminar datos$"), start_delete),
    ],
    states={
        S_REG_ROLE: [
            CallbackQueryHandler(role_chosen, pattern=r"^role:", per_message=False)
        ],
        S_DEL_CONFIRM: [
            CallbackQueryHandler(delete_confirmed, pattern=r"^del:", per_message=False)
        ],
        S_REP_YEAR:  [MessageHandler(INPUT, rep_year)],
        S_REP_MONTH: [MessageHandler(INPUT, rep_month)],
        S_REP_DAY:   [MessageHandler(INPUT, rep_day)],
        S_REP_HOURS: [MessageHandler(INPUT, rep_hours)],
        S_REP_SALES: [MessageHandler(INPUT, rep_sales)],

        S_INF_CHOICE: [
            CallbackQueryHandler(informe_chosen, pattern=r"^inf:", per_message=False)
        ],
        S_MES_YEAR:  [MessageHandler(INPUT, mes_year)],
        S_MES_MONTH: [MessageHandler(INPUT, mes_month)],
        S_TOT_YEAR:  [MessageHandler(INPUT, tot_year)],
        S_TOT_RANGE: [MessageHandler(INPUT, tot_range)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex(r"^❌ Cancelar$"),       cancel),
        MessageHandler(filters.Regex(r"^📝 Registrarse$"),    start_register),
        MessageHandler(filters.Regex(r"^📊 Reportar$"),       start_report),
        MessageHandler(filters.Regex(r"^📈 Informe$"),        start_informe),
        MessageHandler(filters.Regex(r"^🗑 Eliminar datos$"), start_delete),
    ],
    allow_reentry=True,
    per_message=False,
)

# ─────────────────────── SERVIDOR HTTP (para Render) ───────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot de Telegram en marcha")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"Health server escuchando en puerto {port}")
    server.serve_forever()

# ─────────────────────── MAIN ───────────────────────
def main():
    if not TOKEN or TOKEN == "TU_TOKEN_AQUI":
        print("⚠️ No hay token configurado. Define la variable de entorno TOKEN "
              "o edita TOKEN_DEFAULT en el código.")
        return

    init_db()

    if os.environ.get("PORT"):
        threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(conv_handler)
    print("🤖 Bot en marcha...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
