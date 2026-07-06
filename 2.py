import asyncio, base64, httpx, io, json, logging, os, re, sys, tempfile, uuid
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("cli")

BASE_URL = "https://tathya.uidai.gov.in"
HEADERS_TEMPLATE = {
    "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en_IN", "Origin": "https://myaadhaar.uidai.gov.in",
    "Referer": "https://myaadhaar.uidai.gov.in/",
}


async def _req(method: str, path: str, headers: dict, payload: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _save_captcha(img: bytes, tag: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "uidai_captcha")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"captcha_{tag}_{datetime.now():%H%M%S}.jpg")
    with open(p, "wb") as f:
        f.write(img)
    return p


async def _captcha(rid: str, label: str) -> tuple[str, str]:
    headers = {**HEADERS_TEMPLATE, "Appid": "MYAADHAAR", "X-Request-Id": rid,
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    data = await _req("POST", "/audioCaptchaService/api/captcha/v3/generation", headers,
                      {"captchaLength": "6", "captchaType": "2", "audioCaptchaRequired": False})
    txn = data["transactionId"]
    img = base64.b64decode(data["imageBase64"])
    p = _save_captcha(img, label.lower())
    print(f"\n📸 {label} captcha saved to: {p}\n   Open and type the 6 characters.")
    text = input("   Captcha: ").strip()
    if len(text) < 4:
        print("Aborted."); sys.exit(1)
    return txn, text


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
            cid, ctext = await _captcha(rid, "EID retry")
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
        cid, ctext = await _captcha(rid, "download retry")
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


async def main():
    mobile = input("Mobile: ").strip()
    name = input("Full name: ").strip()
    if not mobile or not name:
        print("Required."); sys.exit(1)

    rid = str(uuid.uuid4())
    cid, ct = await _captcha(rid, "EID")
    print("\nSending OTP...")
    otp_txn = await _otp(rid, mobile, name, cid, ct)
    otp = input("\nEnter OTP: ").strip()
    if len(otp) != 6:
        print("Invalid."); sys.exit(1)
    eid = await _verify(rid, mobile, name, otp, otp_txn, cid, ct)
    print(f"\n✅ EID: {eid['eid']}\n   Name: {eid['name']}\n   DOB: {eid['dob']}")

    rid2 = str(uuid.uuid4())
    cid2, ct2 = await _captcha(rid2, "download")
    print("\nSending download OTP...")
    dl_txn = await _dl_otp(rid2, eid["eid"], cid2, ct2)
    otp2 = input("\nEnter download OTP: ").strip()
    if len(otp2) != 6:
        print("Invalid."); sys.exit(1)
    pdf = await _download(rid2, eid["eid"], otp2, dl_txn)

    unlocked, pw = _unlock_pdf(pdf, eid["name"], eid["dob"])
    if pw:
        print(f"   PDF unlocked (password: {pw})")
    else:
        print("   ⚠ Could not unlock PDF")

    dl_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(dl_dir, exist_ok=True)
    f = os.path.join(dl_dir, f"aadhaar_{eid['eid'][:8]}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
    with open(f, "wb") as h:
        h.write(unlocked)
    print(f"\n✅ Saved: {f} ({len(unlocked)} bytes)")

if __name__ == "__main__":
    asyncio.run(main())
