import asyncio
import base64
import io
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, ConversationHandler

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
log = logging.getLogger("aadhaar_bot")

# ===== BOT CONFIGURATION =====
BOT_TOKEN = "8279807712:AAGdgeU6lQ3E1SzybxuD0zZ6LIpFS1AlSpY"

# ===== UIDAI CONSTANTS =====
BASE_URL = "https://tathya.uidai.gov.in"
HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en_IN",
    "Origin": "https://myaadhaar.uidai.gov.in",
    "Referer": "https://myaadhaar.uidai.gov.in/",
}

# ===== CONVERSATION STATES =====
AWAITING_MOBILE, AWAITING_NAME, AWAITING_CAPTCHA, AWAITING_OTP, AWAITING_DOWNLOAD_CAPTCHA, AWAITING_DOWNLOAD_OTP = range(6)

# ===== ORIGINAL UIDAI FUNCTIONS (UNCHANGED) =====

async def _req(method: str, path: str, headers: dict, payload: dict = None) -> dict:
    """Make a request to UIDAI API - ORIGINAL WORKING VERSION."""
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=30)
    if response.status_code != 200:
        raise Exception(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()

def _save_captcha(image_bytes: bytes, tag: str, user_id: str) -> str:
    """Save captcha image to temp file - ORIGINAL WORKING VERSION."""
    temp_dir = os.path.join(tempfile.gettempdir(), "uidai_captcha")
    os.makedirs(temp_dir, exist_ok=True)
    filename = f"captcha_{tag}_{user_id}_{datetime.now().strftime('%H%M%S')}.jpg"
    filepath = os.path.join(temp_dir, filename)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
    return filepath

async def _generate_captcha(request_id: str, label: str, user_id: str) -> tuple:
    """Generate captcha - ORIGINAL WORKING VERSION."""
    headers = {
        **HEADERS_TEMPLATE,
        "Appid": "MYAADHAAR",
        "X-Request-Id": request_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = {
        "captchaLength": "6",
        "captchaType": "2",
        "audioCaptchaRequired": False
    }
    data = await _req("POST", "/audioCaptchaService/api/captcha/v3/generation", headers, payload)
    transaction_id = data["transactionId"]
    image_bytes = base64.b64decode(data["imageBase64"])
    image_path = _save_captcha(image_bytes, label.lower(), str(user_id))
    return transaction_id, image_path

async def _send_otp(request_id: str, mobile: str, name: str, captcha_txn: str, captcha_text: str) -> str:
    """Send OTP - ORIGINAL WORKING VERSION."""
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": request_id}
    payload = {
        "mobileNumber": mobile,
        "dob": None,
        "email": None,
        "name": name,
        "option": "EID",
        "otp": None,
        "otpTxnId": None,
        "captchaTxnId": captcha_txn,
        "captcha": captcha_text,
        "resendOtp": False
    }
    for attempt in range(3):
        data = await _req("POST", "/retrieveEidUid/ext/v1/generic/retrieveuideid", headers, payload)
        response_data = data.get("responseData") or {}
        otp_txn = response_data.get("otpTxnId")
        if otp_txn:
            return otp_txn
        error = data.get("errorCode", "")
        if "CAP" in str(error).upper() or data.get("status") in (400, "400"):
            log.warning("Captcha rejected, retry %d...", attempt + 1)
            continue
        raise Exception(data.get("errorDetails", {}).get("messageEnglish", data.get("message", "OTP failed")))
    raise Exception("OTP failed after 3 attempts")

async def _verify_otp(request_id: str, mobile: str, name: str, otp: str, otp_txn: str, captcha_txn: str, captcha_text: str) -> dict:
    """Verify OTP - ORIGINAL WORKING VERSION."""
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": request_id}
    payload = {
        "mobileNumber": mobile,
        "dob": None,
        "email": None,
        "name": name,
        "option": "EID",
        "otp": otp,
        "otpTxnId": otp_txn,
        "captchaTxnId": captcha_txn,
        "captcha": captcha_text,
        "resendOtp": False
    }
    data = await _req("POST", "/retrieveEidUid/ext/v1/generic/retrieveuideid", headers, payload)
    response_data = data.get("responseData") or {}
    if not response_data.get("eidNumber"):
        raise Exception(response_data.get("message", data.get("message", "EID not found")))
    return {
        "eid": response_data["eidNumber"],
        "name": response_data.get("name", name),
        "dob": response_data.get("dateOfBirth", "")
    }

async def _send_download_otp(request_id: str, eid: str, captcha_txn: str, captcha_text: str) -> str:
    """Send download OTP - ORIGINAL WORKING VERSION."""
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": request_id}
    payload = {
        "eidNumber": eid,
        "idType": "eid",
        "captchaTxnId": captcha_txn,
        "captchaValue": captcha_text,
        "transactionId": request_id,
        "resendOTP": False
    }
    for attempt in range(3):
        data = await _req("POST", "/unifiedAppAuthService/api/v2/generate/aadhaar/otp", headers, payload)
        if data.get("status", "").lower() == "success" and data.get("txnId"):
            return data["txnId"]
        log.warning("Download captcha rejected, retry %d...", attempt + 1)
        continue
    raise Exception("Download OTP failed")

async def _download_aadhaar(request_id: str, eid: str, otp: str, otp_txn: str) -> bytes:
    """Download Aadhaar PDF - ORIGINAL WORKING VERSION."""
    headers = {
        **HEADERS_TEMPLATE,
        "Appid": "MYAADHAAR",
        "X-Request-Id": request_id,
        "Transactionid": request_id
    }
    payload = {
        "eid": eid,
        "mask": False,
        "otp": otp,
        "otpTxnId": otp_txn
    }
    data = await _req("POST", "/downloadAadhaarService/api/aadhaar/download", headers, payload)
    pdf_b64 = data.get("data", {}).get("aadhaarPdf")
    if not pdf_b64:
        raise Exception(data.get("statusMessage", data.get("message", "PDF missing")))
    return base64.b64decode(pdf_b64)

def _unlock_pdf(pdf_bytes: bytes, name_hint: str, dob: str) -> tuple:
    """Attempt to unlock PDF with common password patterns."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return pdf_bytes, None

    prefix = re.sub(r"[^A-Za-z]", "", name_hint or "")[:4].upper()
    if not prefix:
        return pdf_bytes, None

    year_match = re.search(r"(19|20)\d{2}", dob or "")
    years = [year_match.group(0)] if year_match else list(range(1966, 2027))

    for password in [f"{prefix}{year}" for year in years]:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            if reader.is_encrypted and reader.decrypt(password) == 0:
                continue
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            output = io.BytesIO()
            writer.write(output)
            return output.getvalue(), password
        except Exception:
            continue
    return pdf_bytes, None

# ===== TELEGRAM BOT HANDLERS =====

async def start(update: Update, context: CallbackContext) -> int:
    """Start the conversation."""
    user_id = str(update.effective_user.id)
    context.user_data.clear()
    context.user_data["user_id"] = user_id

    await update.message.reply_text(
        "🔐 *Aadhaar Retrieval Bot*\n\n"
        "This bot helps you retrieve your Aadhaar EID or download your Aadhaar PDF.\n"
        "⚠️ *For humanitarian use only.*\n\n"
        "Please enter your **mobile number** (with country code, e.g., 91XXXXXXXXXX):",
        parse_mode="Markdown"
    )
    return AWAITING_MOBILE

async def mobile_input(update: Update, context: CallbackContext) -> int:
    """Handle mobile number input."""
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) < 10:
        await update.message.reply_text("❌ Invalid mobile number. Please enter a valid number (e.g., 91XXXXXXXXXX):")
        return AWAITING_MOBILE

    context.user_data["mobile"] = mobile
    await update.message.reply_text("📝 Please enter your **full name** as per Aadhaar:")
    return AWAITING_NAME

async def name_input(update: Update, context: CallbackContext) -> int:
    """Handle name input and generate captcha."""
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Name too short. Please enter your full name:")
        return AWAITING_NAME

    context.user_data["name"] = name
    request_id = str(uuid.uuid4())
    context.user_data["request_id"] = request_id

    try:
        captcha_txn, image_path = await _generate_captcha(request_id, "EID", context.user_data["user_id"])
        context.user_data["captcha_txn"] = captcha_txn

        with open(image_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 *Please solve this captcha and type the 6 characters.*\n\nType the captcha text in this chat:",
                parse_mode="Markdown"
            )
        return AWAITING_CAPTCHA
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating captcha: {str(e)}\nPlease try again with /start")
        return ConversationHandler.END

async def captcha_input(update: Update, context: CallbackContext) -> int:
    """Handle captcha input and send OTP."""
    captcha_text = update.message.text.strip()
    if len(captcha_text) < 4:
        await update.message.reply_text("❌ Invalid captcha. Please type the 6 characters shown:")
        return AWAITING_CAPTCHA

    context.user_data["captcha_text"] = captcha_text

    try:
        await update.message.reply_text("📤 Sending OTP to your mobile number...")
        otp_txn = await _send_otp(
            context.user_data["request_id"],
            context.user_data["mobile"],
            context.user_data["name"],
            context.user_data["captcha_txn"],
            captcha_text
        )
        context.user_data["otp_txn"] = otp_txn

        await update.message.reply_text(
            "📱 *OTP sent successfully!*\n\nPlease enter the 6-digit OTP you received:",
            parse_mode="Markdown"
        )
        return AWAITING_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ OTP generation failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def otp_input(update: Update, context: CallbackContext) -> int:
    """Handle OTP input and verify."""
    otp = update.message.text.strip()
    if len(otp) != 6:
        await update.message.reply_text("❌ Invalid OTP. Please enter 6 digits:")
        return AWAITING_OTP

    try:
        await update.message.reply_text("🔍 Verifying OTP and retrieving EID...")
        eid_data = await _verify_otp(
            context.user_data["request_id"],
            context.user_data["mobile"],
            context.user_data["name"],
            otp,
            context.user_data["otp_txn"],
            context.user_data["captcha_txn"],
            context.user_data["captcha_text"]
        )
        context.user_data["eid"] = eid_data["eid"]
        context.user_data["eid_name"] = eid_data["name"]
        context.user_data["eid_dob"] = eid_data["dob"]

        await update.message.reply_text(
            f"✅ *EID Retrieved Successfully!*\n\n"
            f"🔹 EID: `{eid_data['eid']}`\n"
            f"🔹 Name: {eid_data['name']}\n"
            f"🔹 DOB: {eid_data['dob']}\n\n"
            "📥 Now initiating download...",
            parse_mode="Markdown"
        )

        # Generate captcha for download
        request_id2 = str(uuid.uuid4())
        context.user_data["request_id2"] = request_id2
        captcha_txn2, image_path2 = await _generate_captcha(request_id2, "download", context.user_data["user_id"])
        context.user_data["captcha_txn2"] = captcha_txn2

        with open(image_path2, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="📸 *New captcha for download.* Please type the 6 characters:",
                parse_mode="Markdown"
            )
        return AWAITING_DOWNLOAD_CAPTCHA
    except Exception as e:
        await update.message.reply_text(f"❌ Verification failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def download_captcha_input(update: Update, context: CallbackContext) -> int:
    """Handle download captcha and send download OTP."""
    captcha_text = update.message.text.strip()
    if len(captcha_text) < 4:
        await update.message.reply_text("❌ Invalid captcha. Please type the 6 characters:")
        return AWAITING_DOWNLOAD_CAPTCHA

    context.user_data["download_captcha"] = captcha_text

    try:
        await update.message.reply_text("📤 Sending download OTP...")
        dl_txn = await _send_download_otp(
            context.user_data["request_id2"],
            context.user_data["eid"],
            context.user_data["captcha_txn2"],
            captcha_text
        )
        context.user_data["dl_txn"] = dl_txn

        await update.message.reply_text(
            "📱 *Download OTP sent!*\n\nPlease enter the 6-digit OTP:",
            parse_mode="Markdown"
        )
        return AWAITING_DOWNLOAD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Download OTP failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def final_download(update: Update, context: CallbackContext) -> int:
    """Handle final download OTP and send PDF."""
    otp = update.message.text.strip()
    if len(otp) != 6:
        await update.message.reply_text("❌ Invalid OTP. Please enter 6 digits:")
        return AWAITING_DOWNLOAD_OTP

    try:
        await update.message.reply_text("📥 Downloading Aadhaar PDF...")
        pdf_bytes = await _download_aadhaar(
            context.user_data["request_id2"],
            context.user_data["eid"],
            otp,
            context.user_data["dl_txn"]
        )

        unlocked_pdf, password = _unlock_pdf(
            pdf_bytes,
            context.user_data.get("eid_name", ""),
            context.user_data.get("eid_dob", "")
        )

        if password:
            await update.message.reply_text(f"🔓 PDF unlocked! Password: `{password}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Could not unlock PDF automatically. You may need to try manually.")

        await update.message.reply_document(
            document=io.BytesIO(unlocked_pdf),
            filename=f"aadhaar_{context.user_data['eid'][:8]}.pdf"
        )

        await update.message.reply_text(
            "✅ *Aadhaar PDF downloaded successfully!*\n\n"
            "⚠️ Please delete this file after use. Keep your data secure.\n\n"
            "For humanitarian use only. 🙏"
        )

        log.info(f"User {context.user_data['user_id']} downloaded Aadhaar: {context.user_data['eid'][:8]}")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Download failed: {str(e)}\nTry again with /start")
        return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the conversation."""
    await update.message.reply_text("❌ Operation cancelled. Use /start to begin again.")
    return ConversationHandler.END

# ===== MAIN FUNCTION =====

def main():
    """Run the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    conversation_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, mobile_input)],
            AWAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_input)],
            AWAITING_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_input)],
            AWAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_input)],
            AWAITING_DOWNLOAD_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, download_captcha_input)],
            AWAITING_DOWNLOAD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, final_download)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(conversation_handler)
    application.add_handler(CommandHandler("start", start))

    log.info("🤖 Bot started. Token: %s", BOT_TOKEN[:10] + "...")
    application.run_polling()

if __name__ == "__main__":
    main()
