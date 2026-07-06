import asyncio, base64, httpx, io, json, logging, os, re, sys, tempfile, uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, ConversationHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("aadhaar_bot")

BOT_TOKEN = "8279807712:AAGdgeU6lQ3E1SzybxuD0zZ6LIpFS1AlSpY"

BASE_URL = "https://tathya.uidai.gov.in"
HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en_IN",
    "Origin": "https://myaadhaar.uidai.gov.in",
    "Referer": "https://myaadhaar.uidai.gov.in/",
}

USER_DATA = {}
AWAITING_MOBILE, AWAITING_NAME, AWAITING_OTP, AWAITING_DOWNLOAD_OTP = range(4)

async def _req(method: str, path: str, headers: dict, payload: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()

def _save_captcha(img: bytes, tag: str, user_id: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "uidai_captcha")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"captcha_{tag}_{user_id}_{datetime.now():%H%M%S}.jpg")
    with open(p, "wb") as f:
        f.write(img)
    return p

async def _captcha(rid: str, label: str, user_id: str) -> tuple[str, str]:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid,
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    data = await _req("POST", "/audioCaptchaService/api/captcha/v3/generation", headers,
                      {"captchaLength": "6", "captchaType": "2", "audioCaptchaRequired": False})
    txn = data["transactionId"]
    img = base64.b64decode(data["imageBase64"])
    p = _save_captcha(img, label.lower(), str(user_id))
    return txn, p

async def _otp(rid: str, mobile: str, name: str, cid: str, ctext: str) -> str:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid}
    for attempt in range(3):
        data = await _req("POST", "/retrieveEidUid/ext/v1/generic/retrieveuideid", headers, {
            "mobileNumber": mobile, "dob": None, "email": None, "name": name, "option": "EID",
            "otp": None, "otpTxnId": None, "captchaTxnId": cid, "captcha": ctext, "resendOtp": False,
        })
        rd = data.get("responseData") or {}
        otp_txn = rd.get("otpTxnId")
        if otp_txn:
            return otp_txn
        err = data.get("errorCode", "")
        if "CAP" in str(err).upper() or data.get("status") in (400, "400"):
            log.warning("Captcha rejected, retry %s...", attempt + 1)
            continue
        raise Exception(data.get("errorDetails", {}).get("messageEnglish", data.get("message", "OTP failed")))
    raise Exception("OTP failed after 3 attempts")

async def _verify(rid: str, mobile: str, name: str, otp: str, otp_txn: str, cid: str, ctext: str) -> dict:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid}
    data = await _req("POST", "/retrieveEidUid/ext/v1/generic/retrieveuideid", headers, {
        "mobileNumber": mobile, "dob": None, "email": None, "name": name, "option": "EID",
        "otp": otp, "otpTxnId": otp_txn, "captchaTxnId": cid, "captcha": ctext, "resendOtp": False,
    })
    rd = data.get("responseData") or {}
    if not rd.get("eidNumber"):
        raise Exception(rd.get("message", data.get("message", "EID not found")))
    return {"eid": rd["eidNumber"], "name": rd.get("name", name), "dob": rd.get("dateOfBirth", "")}

async def _dl_otp(rid: str, eid: str, cid: str, ctext: str) -> str:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid}
    for attempt in range(3):
        data = await _req("POST", "/unifiedAppAuthService/api/v2/generate/aadhaar/otp", headers, {
            "eidNumber": eid, "idType": "eid", "captchaTxnId": cid, "captchaValue": ctext,
            "transactionId": rid, "resendOTP": False,
        })
        if data.get("status", "").lower() == "success" and data.get("txnId"):
            return data["txnId"]
        log.warning("Download captcha rejected, retry %s...", attempt + 1)
        continue
    raise Exception("Download OTP failed")

async def _download(rid: str, eid: str, otp: str, otp_txn: str) -> bytes:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid, "Transactionid": rid}
    data = await _req("POST", "/downloadAadhaarService/api/aadhaar/download", headers, {
        "eid": eid, "mask": False, "otp": otp, "otpTxnId": otp_txn,
    })
    pdf_b64 = data.get("data", {}).get("aadhaarPdf")
    if not pdf_b64:
        raise Exception(data.get("statusMessage", data.get("message", "PDF missing")))
    return base64.b64decode(pdf_b64)

def _unlock_pdf(pdf: bytes, name_hint: str, dob: str) -> tuple[bytes, Optional[str]]:
    from pypdf import PdfReader, PdfWriter
    prefix = re.sub(r"[^A-Za-z]", "", (name_hint or ""))[:4].upper()
    if not prefix:
        return pdf, None
    m = re.search(r"(19|20)\d{2}", dob or "")
    years = [m.group(0)] if m else list(range(1966, 2027))
    for pw in [f"{prefix}{y}" for y in years]:
        try:
            r = PdfReader(io.BytesIO(pdf))
            if r.is_encrypted and r.decrypt(pw) == 0:
                continue
            w = PdfWriter()
            for p in r.pages:
                w.add_page(p)
            buf = io.BytesIO()
            w.write(buf)
            return buf.getvalue(), pw
        except Exception:
            continue
    return pdf, None

async def start(update: Update, context: CallbackContext) -> int:
    user_id = str(update.effective_user.id)
    context.user_data.clear()
    context.user_data["user_id"] = user_id
    await update.message.reply_text(
        "🔐 *Welcome to Aadhaar Retrieval Bot*\n\n"
        "This bot helps you retrieve your Aadhaar EID or download your Aadhaar PDF.\n"
        "⚠️ *For humanitarian use only.*\n\n"
        "Please enter your **mobile number** (with country code, e.g., 91XXXXXXXXXX):",
        parse_mode="Markdown"
    )
    return AWAITING_MOBILE

async def mobile_input(update: Update, context: CallbackContext) -> int:
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) < 10:
        await update.message.reply_text("❌ Invalid mobile number. Please enter a valid number (e.g., 91XXXXXXXXXX):")
        return AWAITING_MOBILE
    context.user_data["mobile"] = mobile
    await update.message.reply_text("📝 Please enter your **full name** as per Aadhaar:")
    return AWAITING_NAME

async def name_input(update: Update, context: CallbackContext) -> int:
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Name too short. Please enter your full name:")
        return AWAITING_NAME
    context.user_data["name"] = name
    rid = str(uuid.uuid4())
    context.user_data["rid"] = rid
    try:
        cid, captcha_path = await _captcha(rid, "EID", context.user_data["user_id"])
        context.user_data["cid"] = cid
        with open(captcha_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 *Please solve this captcha and type the 6 characters.*\n\n"
                        "Type the captcha text in this chat:",
                parse_mode="Markdown"
            )
        return AWAITING_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating captcha: {str(e)}\nPlease try again with /start")
        return ConversationHandler.END

async def otp_input(update: Update, context: CallbackContext) -> int:
    captcha_text = update.message.text.strip()
    if len(captcha_text) < 4:
        await update.message.reply_text("❌ Invalid captcha. Please type the 6 characters shown:")
        return AWAITING_OTP
    context.user_data["captcha_text"] = captcha_text
    try:
        await update.message.reply_text("📤 Sending OTP to your mobile number...")
        otp_txn = await _otp(
            context.user_data["rid"],
            context.user_data["mobile"],
            context.user_data["name"],
            context.user_data["cid"],
            captcha_text
        )
        context.user_data["otp_txn"] = otp_txn
        await update.message.reply_text(
            "📱 *OTP sent successfully!*\n\n"
            "Please enter the 6-digit OTP you received:",
            parse_mode="Markdown"
        )
        return AWAITING_DOWNLOAD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ OTP generation failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def download_otp_input(update: Update, context: CallbackContext) -> int:
    otp = update.message.text.strip()
    if len(otp) != 6:
        await update.message.reply_text("❌ Invalid OTP. Please enter 6 digits:")
        return AWAITING_DOWNLOAD_OTP
    try:
        await update.message.reply_text("🔍 Verifying OTP and retrieving EID...")
        eid_data = await _verify(
            context.user_data["rid"],
            context.user_data["mobile"],
            context.user_data["name"],
            otp,
            context.user_data["otp_txn"],
            context.user_data["cid"],
            context.user_data["captcha_text"]
        )
        context.user_data["eid"] = eid_data["eid"]
        await update.message.reply_text(
            f"✅ *EID Retrieved Successfully!*\n\n"
            f"🔹 EID: `{eid_data['eid']}`\n"
            f"🔹 Name: {eid_data['name']}\n"
            f"🔹 DOB: {eid_data['dob']}\n\n"
            "📥 Now initiating download...",
            parse_mode="Markdown"
        )
        rid2 = str(uuid.uuid4())
        cid2, captcha_path2 = await _captcha(rid2, "download", context.user_data["user_id"])
        context.user_data["rid2"] = rid2
        context.user_data["cid2"] = cid2
        with open(captcha_path2, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 *New captcha for download.* Please type the 6 characters:",
                parse_mode="Markdown"
            )
        context.user_data["awaiting_download_captcha"] = True
        return AWAITING_DOWNLOAD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Verification failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def download_captcha_input(update: Update, context: CallbackContext) -> int:
    captcha_text = update.message.text.strip()
    if len(captcha_text) < 4:
        await update.message.reply_text("❌ Invalid captcha. Please type the 6 characters:")
        return AWAITING_DOWNLOAD_OTP
    try:
        await update.message.reply_text("📤 Sending download OTP...")
        dl_txn = await _dl_otp(
            context.user_data["rid2"],
            context.user_data["eid"],
            context.user_data["cid2"],
            captcha_text
        )
        context.user_data["dl_txn"] = dl_txn
        context.user_data["download_captcha"] = captcha_text
        await update.message.reply_text(
            "📱 *Download OTP sent!*\n\n"
            "Please enter the 6-digit OTP:",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_download_otp"] = True
        return AWAITING_DOWNLOAD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Download OTP failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def final_download_otp(update: Update, context: CallbackContext) -> int:
    otp = update.message.text.strip()
    if len(otp) != 6:
        await update.message.reply_text("❌ Invalid OTP. Please enter 6 digits:")
        return AWAITING_DOWNLOAD_OTP
    try:
        await update.message.reply_text("📥 Downloading Aadhaar PDF...")
        pdf = await _download(
            context.user_data["rid2"],
            context.user_data["eid"],
            otp,
            context.user_data["dl_txn"]
        )
        unlocked, pw = _unlock_pdf(pdf, context.user_data["name"], context.user_data.get("dob", ""))
        if pw:
            await update.message.reply_text(f"🔓 PDF unlocked! Password: `{pw}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Could not unlock PDF automatically. You may need to try manually.")
        await update.message.reply_document(
            document=io.BytesIO(unlocked),
            filename=f"aadhaar_{context.user_data['eid'][:8]}.pdf"
        )
        await update.message.reply_text(
            "✅ *Aadhaar PDF downloaded successfully!*\n\n"
            " 🙏"
        )
        log.info(f"User {context.user_data['user_id']} downloaded Aadhaar: {context.user_data['eid'][:8]}")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Download failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("❌ Operation cancelled. Use /start to begin again.")
    return ConversationHandler.END

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mobile_input)],
            AWAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_input)],
            AWAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_input)],
            AWAITING_DOWNLOAD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, download_otp_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    log.info("🤖 Bot started. Token: 8279807712:AAGdgeU6lQ3E1SzybxuD0zZ6LIpFS1AlSpY")
    app.run_polling()

if __name__ == "__main__":
    main()
