"""
SUNEO local server

Accepts an image + click type from:
  - ESP32-CAM cap : raw JPEG body, headers X-Click-Type, X-Device-Id (optional X-Lang)
  - Web page / Flutter app : multipart form (image, click_type, device_id, lang, lat, lng)

Flow:  receive -> save -> Groq scene description | local faces + currency + OCR.space
       | Telegram SOS -> return result text

Also serves the SUNEO web app at  http://localhost:5000/

Run:   python server.py
"""

import base64
import ipaddress
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

try:
    import cv2
    import numpy as np
except ImportError as e:
    cv2 = None
    np = None
    FACE_IMPORT_ERROR = str(e)
else:
    FACE_IMPORT_ERROR = ""


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ======================= CONFIG =======================
HOST = "0.0.0.0"          # must be 0.0.0.0 so ESP32 / phone can reach it
PORT = 5000

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "220"))
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "20"))            # a live device should not wait minutes
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "").strip()
OCR_SPACE_TIMEOUT = float(os.getenv("OCR_SPACE_TIMEOUT", "30"))
OCR_TESTING_DISABLED = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_TIMEOUT = 8       # per attempt; 2 attempts must fit inside the cap's 20 s HTTP timeout
SOS_LOCATION_NOTE = os.getenv("SOS_LOCATION_NOTE", "").strip()   # used when the sender has no GPS (the cap)

# Language used when a device does not send one (the cap never does). "en" or "hi".
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en").strip().lower()[:2]
if DEFAULT_LANG not in {"en", "hi"}:
    DEFAULT_LANG = "en"

# User-facing errors are short and friendly. Set SUNEO_VERBOSE_ERRORS=1 to include technical details.
VERBOSE_ERRORS = env_flag("SUNEO_VERBOSE_ERRORS")

# Captured photos and /api/history are private: by default only the server PC itself can open them.
ALLOW_LAN_VIEW = env_flag("SUNEO_ALLOW_LAN_VIEW")
ALLOW_LAN_RESULTS = env_flag("SUNEO_ALLOW_LAN_RESULTS")
CAPTURE_RETENTION_DAYS = int(os.getenv("CAPTURE_RETENTION_DAYS", "7"))   # 0 = keep forever

# Images sent to Groq / OCR.space / Telegram are shrunk to fit their size limits
# (OCR.space free = 1 MB, Groq base64 = 4 MB).
SERVICE_IMAGE_MAX_EDGE = int(os.getenv("SERVICE_IMAGE_MAX_EDGE", "1280"))
SERVICE_IMAGE_MAX_BYTES = 900_000

# Local face branch
ENABLE_LOCAL_FACES = True

SAVE_DIR = BASE_DIR / "captures"
KNOWN_FACES_DIR = BASE_DIR / "known_faces"
KNOWN_CURRENCY_DIR = BASE_DIR / "known_currency"
FACE_RECOGNITION_THRESHOLD = 70.0
MIN_CURRENCY_MATCHES = 12
CLICK_ALIASES = {
    "scene": "scene",
    "groq": "scene",
    "gemini": "scene",
    "one": "scene",
    "1": "scene",
    "ocr": "ocr",
    "local": "ocr",
    "two": "ocr",
    "2": "ocr",
    "sos": "sos",
}
CAP_PREFIXES = ("cap",)    # device ids that count as the physical cap in the UI
NO_TEXT = "No text found in the image."
# ======================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("suneo")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
SAVE_DIR.mkdir(exist_ok=True)

HISTORY: "deque[dict]" = deque(maxlen=30)   # newest first, shown in the web UI
HISTORY_LOCK = threading.Lock()
DEVICES: dict = {}                          # device_id -> last seen (epoch seconds)


# ---------------- spoken / shown messages ----------------
MESSAGES = {
    "en": {
        "scene_fail": "Sorry, I could not describe the scene. Please try again.",
        "face_fail": "Face recognition is unavailable right now.",
        "currency_fail": "Currency recognition is unavailable right now.",
        "text_fail": "Text reading is unavailable right now.",
        "sos_ok": "SOS alert sent.",
        "sos_fail": "SOS could not be sent. Please try again.",
        "nothing": "No known face, currency note, or readable text was detected.",
        "people": "People: {names}.",
        "unknown_person": "Unknown person detected.",
        "currency": "Currency note: {value} rupees.",
        "text_prefix": "Text: ",
        "ocr_disabled": "OCR is paused for testing.",
    },
    "hi": {
        "scene_fail": "माफ़ कीजिए, मैं दृश्य नहीं बता पाया। कृपया फिर से कोशिश करें।",
        "face_fail": "चेहरा पहचानने में अभी दिक्कत है।",
        "currency_fail": "नोट पहचानने में अभी दिक्कत है।",
        "text_fail": "टेक्स्ट पढ़ने में अभी दिक्कत है।",
        "sos_ok": "SOS संदेश भेज दिया गया है।",
        "sos_fail": "SOS नहीं भेजा जा सका। कृपया फिर से कोशिश करें।",
        "nothing": "कोई जाना-पहचाना चेहरा, नोट या पढ़ने लायक टेक्स्ट नहीं मिला।",
        "people": "लोग: {names}।",
        "unknown_person": "अनजान व्यक्ति दिखा।",
        "currency": "नोट: {value} रुपये।",
        "text_prefix": "टेक्स्ट: ",
        "ocr_disabled": "टेस्टिंग के लिए OCR रोक दिया गया है।",
    },
}


def msg(lang: str, key: str, **kwargs) -> str:
    return MESSAGES.get(lang, MESSAGES["en"])[key].format(**kwargs)


def friendly(lang: str, key: str, detail: str) -> str:
    """Short message for the user; technical detail only when SUNEO_VERBOSE_ERRORS=1."""
    text = msg(lang, key)
    return f"{text} ({detail})" if VERBOSE_ERRORS else text


# ---------------- local face recognition ----------------
class LocalFaceRecognizer:
    IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}

    def __init__(self):
        self._lock = threading.RLock()
        self._cascade = None
        self._recognizer = None
        self._labels = {}
        self.error = None

        if cv2 is None:
            self.error = f"OpenCV is unavailable: {FACE_IMPORT_ERROR}"
            log.error(self.error)
            return

        try:
            if not hasattr(cv2, "face"):
                raise RuntimeError("OpenCV face module is unavailable; install opencv-contrib-python-headless")
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            self._cascade = cv2.CascadeClassifier(str(cascade_path))
            if self._cascade.empty():
                raise RuntimeError(f"Could not load face detector: {cascade_path}")
            self.reload()
        except Exception as e:
            self.error = str(e)
            log.exception("Local face recognition could not be initialized")

    @property
    def available(self):
        return self._cascade is not None and self.error is None

    @property
    def people_count(self):
        return len(self._labels)

    def _face_boxes(self, gray, allow_relaxed=False):
        boxes = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40)
        )
        if len(boxes) or not allow_relaxed:
            return boxes
        return self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3, minSize=(30, 30)
        )

    def _face_frame_and_boxes(self, gray, allow_relaxed=False):
        rotated = cv2.rotate(gray, cv2.ROTATE_180)
        candidates = (
            gray,
            rotated,
            cv2.equalizeHist(gray),
            cv2.equalizeHist(rotated),
        )
        for frame in candidates:
            boxes = self._face_boxes(frame, allow_relaxed=allow_relaxed)
            if len(boxes):
                return frame, boxes
        return gray, ()

    @staticmethod
    def _prepare_face(gray, box):
        x, y, width, height = box
        face = gray[y:y + height, x:x + width]
        face = cv2.equalizeHist(face)
        return cv2.resize(face, (200, 200))

    def reload(self):
        with self._lock:
            samples = []
            sample_labels = []
            labels = {}
            label_id = 0

            KNOWN_FACES_DIR.mkdir(parents=True, exist_ok=True)
            for person_dir in sorted(KNOWN_FACES_DIR.iterdir()):
                if not person_dir.is_dir() or person_dir.name.startswith("."):
                    continue
                person_samples = []
                for image_path in sorted(person_dir.iterdir()):
                    if image_path.suffix.lower() not in self.IMAGE_SUFFIXES:
                        continue
                    image = cv2.imread(str(image_path))
                    if image is None:
                        log.warning("Skipping unreadable face image: %s", image_path)
                        continue
                    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    gray, boxes = self._face_frame_and_boxes(gray)
                    if len(boxes):
                        largest = max(boxes, key=lambda box: box[2] * box[3])
                        person_samples.append(self._prepare_face(gray, largest))

                if person_samples:
                    labels[label_id] = person_dir.name
                    samples.extend(person_samples)
                    sample_labels.extend([label_id] * len(person_samples))
                    label_id += 1

            self._recognizer = None
            self._labels = labels
            if samples:
                recognizer = cv2.face.LBPHFaceRecognizer_create()
                recognizer.train(samples, np.asarray(sample_labels, dtype=np.int32))
                self._recognizer = recognizer
            self.error = None
            log.info("Loaded local face data for %d people", len(labels))

    def recognize(self, image_bytes: bytes) -> list:
        with self._lock:
            if not self.available:
                raise RuntimeError(self.error or "Local face recognition is unavailable")

            image_array = np.frombuffer(image_bytes, dtype=np.uint8)
            image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("Could not decode image for face recognition")

            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            gray, boxes = self._face_frame_and_boxes(gray, allow_relaxed=True)
            results = []
            for box in boxes:
                prepared = self._prepare_face(gray, box)
                name = "unknown"
                confidence = 0.0
                if self._recognizer is not None:
                    label, distance = self._recognizer.predict(prepared)
                    if label in self._labels and distance <= FACE_RECOGNITION_THRESHOLD:
                        name = self._labels[label]
                        confidence = max(0.0, 1.0 - distance / FACE_RECOGNITION_THRESHOLD)
                results.append({"name": name, "confidence": round(confidence, 3)})
            return results


KNOWN_FACES_DIR.mkdir(parents=True, exist_ok=True)
FACE_RECOGNIZER = LocalFaceRecognizer()


def detect_faces(image_bytes: bytes) -> list:
    return FACE_RECOGNIZER.recognize(image_bytes)


# ---------------- local currency recognition ----------------
class LocalCurrencyRecognizer:
    IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}

    def __init__(self):
        self._lock = threading.RLock()
        self._orb = cv2.ORB_create(nfeatures=1800) if cv2 is not None else None
        self._templates = []
        self.error = None
        self.reload()

    @property
    def available(self):
        return self._orb is not None and self.error is None

    @property
    def denominations_count(self):
        return len({template["denomination"] for template in self._templates})

    def reload(self):
        with self._lock:
            templates = []
            KNOWN_CURRENCY_DIR.mkdir(parents=True, exist_ok=True)
            if self._orb is None:
                self.error = f"OpenCV is unavailable: {FACE_IMPORT_ERROR}"
                log.error("Local currency recognition failed: %s", self.error)
                return

            try:
                for denomination_dir in sorted(KNOWN_CURRENCY_DIR.iterdir()):
                    if not denomination_dir.is_dir() or denomination_dir.name.startswith("."):
                        continue
                    for image_path in sorted(denomination_dir.iterdir()):
                        if image_path.suffix.lower() not in self.IMAGE_SUFFIXES:
                            continue
                        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                        if image is None:
                            log.warning("Skipping unreadable currency image: %s", image_path)
                            continue
                        keypoints, descriptors = self._orb.detectAndCompute(image, None)
                        if descriptors is not None:
                            templates.append({
                                "denomination": denomination_dir.name,
                                "keypoints": keypoints,
                                "descriptors": descriptors,
                            })
                self._templates = templates
                self.error = None
                log.info(
                    "Loaded %d local currency note photos for %d denominations",
                    len(templates), self.denominations_count,
                )
            except Exception as e:
                self.error = str(e)
                log.exception("Could not load local currency note photos")

    def recognize(self, image_bytes: bytes) -> list:
        with self._lock:
            if not self.available:
                raise RuntimeError(self.error or "Local currency recognition is unavailable")
            if not self._templates:
                return []

            image_array = np.frombuffer(image_bytes, dtype=np.uint8)
            image = cv2.imdecode(image_array, cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError("Could not decode image for local currency recognition")

            image_keypoints, descriptors = self._orb.detectAndCompute(image, None)
            if descriptors is None:
                return []

            matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
            denomination_scores = {}
            for template in self._templates:
                pairs = matcher.knnMatch(template["descriptors"], descriptors, k=2)
                good_matches = []
                for pair in pairs:
                    if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                        good_matches.append(pair[0])
                score = 0
                if len(good_matches) >= 4:
                    template_points = np.float32([
                        template["keypoints"][match.queryIdx].pt for match in good_matches
                    ]).reshape(-1, 1, 2)
                    image_points = np.float32([
                        image_keypoints[match.trainIdx].pt for match in good_matches
                    ]).reshape(-1, 1, 2)
                    _, inliers = cv2.findHomography(
                        template_points, image_points, cv2.RANSAC, 5.0
                    )
                    if inliers is not None:
                        score = int(inliers.sum())
                current = denomination_scores.get(template["denomination"], 0)
                denomination_scores[template["denomination"]] = max(current, score)

            matches = [
                {"denomination": name, "matches": score}
                for name, score in denomination_scores.items()
                if score >= MIN_CURRENCY_MATCHES
            ]
            matches.sort(key=lambda result: result["matches"], reverse=True)
            return matches[:1]


CURRENCY_RECOGNIZER = LocalCurrencyRecognizer()


def detect_currency(image_bytes: bytes) -> list:
    return CURRENCY_RECOGNIZER.recognize(image_bytes)


def analyze_local_features(image: bytes, lang: str) -> tuple[list, list, list, list]:
    faces = []
    currency = []
    parts = []
    errors = []

    if ENABLE_LOCAL_FACES:
        try:
            faces = detect_faces(image)
            names = [face["name"] for face in faces if face["name"] != "unknown"]
            if names:
                parts.append(msg(lang, "people", names=", ".join(names)))
            elif faces:
                parts.append(msg(lang, "unknown_person"))
        except Exception as e:
            log.error("Local face recognition failed: %s", e)
            errors.append(f"Face recognition error: {e}")
            parts.append(friendly(lang, "face_fail", str(e)))

    if CURRENCY_RECOGNIZER.available:
        try:
            currency = detect_currency(image)
            if currency:
                value = currency[0]["denomination"].lstrip("₹").strip()
                parts.append(msg(lang, "currency", value=value))
        except Exception as e:
            log.error("Local currency recognition failed: %s", e)
            errors.append(f"Currency recognition error: {e}")
            parts.append(friendly(lang, "currency_fail", str(e)))
    elif CURRENCY_RECOGNIZER.error:
        log.error("Local currency recognition failed: %s", CURRENCY_RECOGNIZER.error)
        errors.append(f"Currency recognition error: {CURRENCY_RECOGNIZER.error}")
        parts.append(friendly(lang, "currency_fail", CURRENCY_RECOGNIZER.error))

    return faces, currency, parts, errors


# ---------------- image preparation for online services ----------------
def response_error(service: str, response: requests.Response) -> str:
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("description") or "")
            elif isinstance(error, str):
                message = error
    except ValueError:
        pass
    suffix = f": {message[:300]}" if message else ""
    return f"{service} returned HTTP {response.status_code}{suffix}"


def detect_image_mime_type(image: bytes, reported_type: str = "") -> str:
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image.startswith(b"BM"):
        return "image/bmp"
    if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
        return "image/webp"
    supported = {"image/jpeg", "image/png", "image/gif", "image/bmp", "image/webp"}
    return reported_type if reported_type in supported else "image/jpeg"


def prepare_service_image(image: bytes, mime_type: str) -> tuple[bytes, str]:
    """
    Return an image the online services accept: jpeg/png/webp and small enough.
    Big phone photos are downscaled and recompressed to JPEG (OCR.space free = 1 MB,
    Groq base64 = 4 MB). Small ESP32 frames pass through untouched.
    """
    ok_type = mime_type in {"image/jpeg", "image/png", "image/webp"}
    if ok_type and len(image) <= SERVICE_IMAGE_MAX_BYTES:
        return image, mime_type
    if cv2 is None or np is None:
        if ok_type:
            return image, mime_type          # cannot shrink without OpenCV; try as is
        raise RuntimeError("OpenCV is required to convert this image format.")

    decoded = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError("Could not decode image for service upload")

    height, width = decoded.shape[:2]
    edge = SERVICE_IMAGE_MAX_EDGE
    while True:
        scale = min(1.0, edge / max(height, width))
        frame = decoded
        if scale < 1.0:
            frame = cv2.resize(
                decoded, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
            )
        for quality in (85, 75, 65, 50):
            success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if success and len(encoded) <= SERVICE_IMAGE_MAX_BYTES:
                return encoded.tobytes(), "image/jpeg"
        if edge <= 320:
            raise RuntimeError("Could not shrink image enough for service upload")
        edge = int(edge * 0.75)


# ---------------- Groq scene description ----------------
def groq_image_text(image: bytes, prompt: str, mime_type: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("Groq is not configured; set GROQ_API_KEY in .env.")
    if not GROQ_MODEL:
        raise RuntimeError("Groq is not configured; set GROQ_MODEL.")
    image, mime_type = prepare_service_image(image, mime_type)

    url = "https://api.groq.com/openai/v1/chat/completions"
    image_data = base64.b64encode(image).decode("ascii")
    payload = {
        "model": GROQ_MODEL,
        "max_tokens": GROQ_MAX_TOKENS,
        "reasoning_effort": "none",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_data}"},
                },
            ],
        }],
    }

    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
            timeout=GROQ_TIMEOUT,
        )
    except requests.Timeout as e:
        raise RuntimeError(
            f"Groq request timed out after {GROQ_TIMEOUT:g} seconds. "
            "Check your internet connection and try again."
        ) from e
    except requests.RequestException as e:
        raise RuntimeError(f"Groq request failed: {type(e).__name__}") from e

    if not response.ok:
        raise RuntimeError(response_error("Groq", response))
    try:
        body = response.json()
    except ValueError as e:
        raise RuntimeError("Groq returned invalid JSON.") from e

    choices = body.get("choices", []) if isinstance(body, dict) else []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("Groq returned no scene description.")
    message = choices[0].get("message", {})
    text = message.get("content") if isinstance(message, dict) else None
    if isinstance(text, list):
        text = " ".join(
            item["text"].strip()
            for item in text
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        )
    if isinstance(text, str):
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)   # in case a model leaks its reasoning
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Groq returned no scene description.")
    return text.strip()


def describe_scene(image: bytes, language: str, mime_type: str) -> str:
    prompt = (
        "Describe the image clearly and concisely for a person who is blind or has "
        "low vision. Mention nearby objects, people, text that is clearly legible, "
        "and important hazards. Do not guess uncertain details. Reply in "
        f"{'Hindi' if language == 'hi' else 'English'}."
    )
    return groq_image_text(image, prompt, mime_type)


# ---------------- OCR.space ----------------
def recognize_text(image: bytes, language: str, mime_type: str) -> str:
    if not OCR_SPACE_API_KEY:
        raise RuntimeError("OCR.space is not configured; set OCR_SPACE_API_KEY in .env.")
    if language not in {"en", "hi"}:
        raise ValueError(f"Unsupported OCR language: {language}")

    image, mime_type = prepare_service_image(image, mime_type)
    extension = {
        "image/png": "png",
        "image/webp": "webp",
    }.get(mime_type, "jpg")

    try:
        response = requests.post(
            "https://api.ocr.space/parse/image",
            headers={"apikey": OCR_SPACE_API_KEY},
            data={
                "language": "hin" if language == "hi" else "eng",
                "isOverlayRequired": "false",
                # Engine 2 only supports Latin-script languages; Hindi needs Engine 3.
                "OCREngine": "3" if language == "hi" else "2",
            },
            files={"file": (f"capture.{extension}", image, mime_type)},
            timeout=OCR_SPACE_TIMEOUT,
        )
    except requests.Timeout as e:
        raise RuntimeError(
            f"OCR.space request timed out after {OCR_SPACE_TIMEOUT:g} seconds."
        ) from e
    except requests.RequestException as e:
        raise RuntimeError(f"OCR.space request failed: {type(e).__name__}") from e

    if not response.ok:
        raise RuntimeError(response_error("OCR.space", response))
    try:
        body = response.json()
    except ValueError as e:
        raise RuntimeError("OCR.space returned invalid JSON.") from e
    if not isinstance(body, dict):
        raise RuntimeError("OCR.space returned an invalid response.")

    if body.get("IsErroredOnProcessing"):
        details = body.get("ErrorMessage") or body.get("ErrorDetails") or ""
        if isinstance(details, list):
            details = "; ".join(str(detail) for detail in details)
        details = str(details).strip()
        suffix = f": {details[:300]}" if details else ""
        raise RuntimeError(f"OCR.space could not process the image{suffix}")

    parsed_results = body.get("ParsedResults")
    if not isinstance(parsed_results, list):
        raise RuntimeError("OCR.space returned no parsed results.")
    text = "\n".join(
        result["ParsedText"].strip()
        for result in parsed_results
        if isinstance(result, dict)
        and isinstance(result.get("ParsedText"), str)
        and result["ParsedText"].strip()
    )
    return text or NO_TEXT


# ---------------- Telegram SOS ----------------
def sos_caption(device: str, extra: dict) -> str:
    caption = f"SUNEO SOS alert from {device} at {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}"
    try:
        lat, lng = float(extra.get("lat", "")), float(extra.get("lng", ""))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return caption + f"\nLocation: https://maps.google.com/?q={lat:.6f},{lng:.6f}"
    except (TypeError, ValueError):
        pass
    if SOS_LOCATION_NOTE:
        caption += f"\nLocation note: {SOS_LOCATION_NOTE}"
    return caption


def send_telegram_sos(image: bytes, device: str, mime_type: str, extra: dict) -> str:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Telegram SOS is not configured; set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    caption = sos_caption(device, extra)
    image, mime_type = prepare_service_image(image, mime_type)
    extension = {
        "image/png": "png",
        "image/webp": "webp",
    }.get(mime_type, "jpg")

    response = None
    last_error = None
    for attempt in (1, 2):                       # one retry for network errors / Telegram 5xx
        try:
            response = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": (f"sos.{extension}", image, mime_type)},
                timeout=TELEGRAM_TIMEOUT,
            )
        except requests.RequestException as e:
            last_error = RuntimeError(f"Telegram SOS request failed: {type(e).__name__}")
            response = None
        else:
            if response.status_code < 500:
                break
            last_error = RuntimeError(response_error("Telegram SOS", response))
            response = None
        log.warning("Telegram SOS attempt %d failed: %s", attempt, last_error)
        if attempt == 1:
            time.sleep(1)
    if response is None:
        raise last_error

    if not response.ok:
        if response.status_code == 404:
            raise RuntimeError(
                "Telegram returned HTTP 404. Check that TELEGRAM_BOT_TOKEN in .env "
                "is the complete token from BotFather, without a 'bot' prefix or URL."
            )
        raise RuntimeError(response_error("Telegram SOS", response))
    try:
        body = response.json()
    except ValueError as e:
        raise RuntimeError("Telegram returned invalid JSON for the SOS request.") from e
    if not isinstance(body, dict) or not body.get("ok"):
        description = body.get("description", "") if isinstance(body, dict) else ""
        suffix = f": {str(description)[:300]}" if description else ""
        raise RuntimeError(f"Telegram rejected the SOS request{suffix}")
    return "SOS alert and image sent to Telegram."


# ---------------- pipeline ----------------
def process(image: bytes, click: str, device: str, extra: dict, entry: dict) -> dict:
    lang = extra.get("lang") or DEFAULT_LANG
    mime = extra.get("image_mime_type", "image/jpeg")

    faces = []
    currency = []
    errors = []
    ocr_text = ""
    groq_output = ""
    text = ""

    if click == "scene":
        faces, currency, parts, feature_errors = analyze_local_features(image, lang)
        errors.extend(feature_errors)
        try:
            groq_output = describe_scene(image, lang, mime)
            text = "\n".join([*parts, groq_output])
        except Exception as e:
            log.error("Groq scene description failed: %s", e)
            errors.append(f"Groq error: {e}")
            text = "\n".join([*parts, friendly(lang, "scene_fail", str(e))])

    elif click == "ocr":
        faces, currency, parts, feature_errors = analyze_local_features(image, lang)
        errors.extend(feature_errors)

        if OCR_TESTING_DISABLED:
            parts.append(msg(lang, "ocr_disabled"))
        else:
            try:
                ocr_text = recognize_text(image, lang, mime)
                if ocr_text != NO_TEXT:
                    parts.append(msg(lang, "text_prefix") + ocr_text)
            except Exception as e:
                log.error("OCR.space OCR failed: %s", e)
                errors.append(f"OCR error: {e}")
                parts.append(friendly(lang, "text_fail", str(e)))

        if not parts:
            parts.append(msg(lang, "nothing"))
        text = "\n".join(parts)

    elif click == "sos":
        try:
            send_telegram_sos(image, device, mime, extra)
            text = msg(lang, "sos_ok")
        except Exception as e:
            log.error("Telegram SOS failed: %s", e)
            errors.append(f"Telegram SOS error: {e}")
            text = friendly(lang, "sos_fail", str(e))

    with HISTORY_LOCK:
        entry.update({
            "faces": faces,
            "currency": currency,
            "ocr_text": ocr_text,
            "groq_output": groq_output,
            "errors": errors,
            "status": "error" if errors else "done",
            "text": text,
        })
    log.info("[%s/%s] completed with %d error(s)", device, click, len(errors))

    return dict(entry)


# ---------------- request parsing ----------------
EXTRA_FIELDS = ("focus", "lang", "lat", "lng")


def read_request():
    """Returns (image_bytes, click_type, device_id, extra, is_multipart)."""
    extra = {k: (request.form.get(k) or request.args.get(k) or request.headers.get("X-" + k.capitalize()) or "")
             for k in EXTRA_FIELDS}
    lang = (extra["lang"] or DEFAULT_LANG).strip().lower()[:2]      # "hi-IN" -> "hi"
    extra["lang"] = lang if lang in {"en", "hi"} else DEFAULT_LANG

    if request.files:                                   # web page / Flutter (multipart)
        f = request.files.get("image") or next(iter(request.files.values()))
        image = f.read()
        extra["image_mime_type"] = detect_image_mime_type(image, f.mimetype)
        click = request.form.get("click_type") or request.headers.get("X-Click-Type")
        device = request.form.get("device_id") or request.headers.get("X-Device-Id") or "app"
        return image, (click or "scene").lower(), device, extra, True

    image = request.get_data()                          # ESP32-CAM (raw JPEG)
    extra["image_mime_type"] = detect_image_mime_type(image, request.mimetype or "")
    click = request.headers.get("X-Click-Type")
    device = request.headers.get("X-Device-Id", "unknown")
    return image, (click or "scene").lower(), device, extra, False


# ---------------- privacy: who may see captures and history ----------------
def request_is_local() -> bool:
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


@app.before_request
def guard_private_views():
    if request.path.startswith("/captures/") or request.path == "/api/history":
        if not (ALLOW_LAN_VIEW or request_is_local()):
            return jsonify({"error": "captures and history can only be viewed on the server PC"}), 403
    elif request.path == "/api/results":
        if not (ALLOW_LAN_VIEW or ALLOW_LAN_RESULTS or request_is_local()):
            return jsonify({"error": "LAN result access is disabled"}), 403


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "image too large"}), 413


def cleanup_old_captures():
    if CAPTURE_RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - CAPTURE_RETENTION_DAYS * 86400
    removed = 0
    for path in SAVE_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("Deleted %d captures older than %d days", removed, CAPTURE_RETENTION_DAYS)


# ---------------- routes: web app ----------------
@app.get("/")
def home():
    for folder in (BASE_DIR, BASE_DIR / "static"):
        if (folder / "index.html").exists():
            return send_from_directory(folder, "index.html")
    return "index.html not found next to server.py", 404


@app.get("/captures/<path:name>")
def captured_image(name):
    return send_from_directory(SAVE_DIR, name)


@app.get("/api/history")
def api_history():
    with HISTORY_LOCK:
        history = [dict(item) for item in HISTORY]
    return jsonify(history)


@app.get("/api/results")
def api_results():
    after_id = request.args.get("after_id", "0")
    try:
        after_id = int(after_id)
    except ValueError:
        return jsonify({"error": "after_id must be an integer"}), 400

    device = request.args.get("device_id", "").strip()
    with HISTORY_LOCK:
        history = list(HISTORY)
    results = [
        {
            "id": item["id"],
            "device": item["device"],
            "click_type": item["click_type"],
            "status": item["status"],
            "text": item["text"],
            "errors": item.get("errors", []),
        }
        for item in reversed(history)
        if item["id"] > after_id
        and item["status"] in {"done", "error"}
        and (not device or item["device"] == device)
    ]
    return jsonify({"results": results})


@app.get("/api/status")
def api_status():
    now = datetime.now().timestamp()
    cap_seen = [now - t for d, t in DEVICES.items() if d.startswith(CAP_PREFIXES)]
    return jsonify({
        "status": "ok",
        "groq_configured": bool(GROQ_API_KEY),
        "groq_model": GROQ_MODEL,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "face_recognition_available": FACE_RECOGNIZER.available,
        "face_recognition_ready": FACE_RECOGNIZER.available and FACE_RECOGNIZER.people_count > 0,
        "known_people_count": FACE_RECOGNIZER.people_count,
        "face_recognition_error": FACE_RECOGNIZER.error,
        "currency_recognition_available": CURRENCY_RECOGNIZER.available,
        "currency_recognition_error": CURRENCY_RECOGNIZER.error,
        "known_currency_count": CURRENCY_RECOGNIZER.denominations_count,
        "currency_recognition_ready": CURRENCY_RECOGNIZER.available and CURRENCY_RECOGNIZER.denominations_count > 0,
        "ocr_available": not OCR_TESTING_DISABLED and bool(OCR_SPACE_API_KEY),
        "ocr_disabled_for_testing": OCR_TESTING_DISABLED,
        "ocr_provider": "OCR.space",
        "cap_seconds_ago": round(min(cap_seen)) if cap_seen else None,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "groq_configured": bool(GROQ_API_KEY),
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    })


# ---------------- routes: capture ----------------
@app.post("/capture")
def capture():
    image, click, device, extra, multipart = read_request()

    if not image:
        return jsonify({"error": "empty image"}), 400
    click = CLICK_ALIASES.get(click)
    if click is None:
        return jsonify({"error": "click_type must identify one-click scene, two-click local analysis, or SOS"}), 400

    now = datetime.now()
    extension = {
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }.get(extra["image_mime_type"], "jpg")
    safe_device = re.sub(r"[^A-Za-z0-9_-]", "_", device)[:40]
    name = f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{safe_device}_{click}.{extension}"
    (SAVE_DIR / name).write_bytes(image)
    DEVICES[device] = now.timestamp()
    log.info("Got %s from %s (%d bytes) -> %s", click, device, len(image), name)

    entry = {
        "id": int(now.timestamp() * 1000),
        "time": now.strftime("%H:%M:%S"),
        "device": device,
        "source": "cap" if device.startswith(CAP_PREFIXES) else "web",
        "click_type": click,
        "focus": extra.get("focus", ""),
        "image": f"/captures/{name}",
        "status": "processing",
        "text": "",
    }
    with HISTORY_LOCK:
        HISTORY.appendleft(entry)

    # Web page / app, SOS, or ?sync=1 wait for the real result. SOS must not report
    # success to the cap before Telegram has actually accepted the alert.
    if multipart or click == "sos" or request.args.get("sync") == "1":
        result = process(image, click, device, extra, entry)
        # the cap's firmware reads a non-2xx reply as "failed" and plays the long error beep
        failed_sos_from_cap = click == "sos" and not multipart and result["status"] == "error"
        return jsonify(result), (502 if failed_sos_from_cap else 200)

    threading.Thread(target=process, args=(image, click, device, extra, entry), daemon=True).start()
    return jsonify({"status": "accepted", "click_type": click}), 202


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "unknown"


if __name__ == "__main__":
    cleanup_old_captures()
    ip = lan_ip()
    log.info("Web app (this PC):   http://localhost:%d/", PORT)
    log.info("Put this in suneo.ino -> SERVER_URL = \"http://%s:%d/capture\"", ip, PORT)
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set: one-click scene descriptions are unavailable")
    if not OCR_SPACE_API_KEY:
        log.warning("OCR_SPACE_API_KEY not set: two-click text reading is unavailable")
    if OCR_TESTING_DISABLED:
        log.info("OCR_TESTING_DISABLED is enabled: OCR.space requests are paused")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram SOS is not configured")
    if not ALLOW_LAN_VIEW:
        log.info("Captures/history are visible only on this PC (set SUNEO_ALLOW_LAN_VIEW=1 to open them to the LAN)")
    if not ALLOW_LAN_VIEW and not ALLOW_LAN_RESULTS:
        log.info("LAN result polling is disabled (set SUNEO_ALLOW_LAN_RESULTS=1 to allow text-only results)")
    app.run(host=HOST, port=PORT, threaded=True)
