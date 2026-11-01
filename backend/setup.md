# Server setup

The cap's click types and web actions map to these server-side operations:

| Action | Click type | Processing |
|---|---|---|
| One click | `scene` | Checks faces and currency locally, then requests a Groq scene description |
| Two clicks | `ocr` | Local face recognition and currency-note matching; OCR.space text recognition |
| SOS | `sos` | Sends the captured image and alert to Telegram |

The server also accepts aliases `groq`/`one`/`1` for `scene` and
`local`/`two`/`2` for `ocr`. Scene descriptions require internet access and send
the image to Groq. Face recognition and currency matching run locally.
Both scene and OCR actions run local face and currency recognition. The cap's
one-click scene response therefore includes known faces and matched denominations,
in addition to the Groq description.
Two-click OCR sends the image to OCR.space, so images used for that action leave your
server. SOS sends the photo to the Telegram chat configured below.
OCR.space requests are temporarily disabled during feature testing; find
`OCR_TESTING_DISABLED` in `server.py` and set it to `False` when ready to test OCR again.

## Install

```bash
python -m pip install -r requirements.txt
```

The page's English/Hindi selector chooses the OCR language. A device can send
`X-Lang: en` or `X-Lang: hi`. The OCR status pill indicates whether an OCR.space API key
is configured.

## Configure Groq

Copy `.env.example` to `.env` in the project directory, then add your Groq API key
and Telegram bot details:

```bash
cp .env.example .env
```

Set `GROQ_API_KEY` in `.env` to your key from the [GroqCloud console](https://console.groq.com/keys).
It is used only for one-click scene descriptions. `GROQ_MODEL` defaults to
`qwen/qwen3.8-27b`, a vision-capable model listed in Groq's [vision documentation](https://console.groq.com/docs/vision).
Scene requests set `reasoning_effort` to `none` for instruct mode. `GROQ_MAX_TOKENS`
limits the generated response and defaults to `220`. `GROQ_TIMEOUT` controls how long
the server waits for Groq in seconds and defaults to `120`. The key is sent in the API
authorization header and is not included in server responses or logs.

## Configure online OCR

Register for an OCR.space API key at [ocr.space/ocrapi](https://ocr.space/ocrapi), then set
`OCR_SPACE_API_KEY` in `.env`. Two-click OCR uploads its image to OCR.space; face recognition
and currency matching still run locally. `OCR_SPACE_TIMEOUT` controls the request timeout
in seconds and defaults to `60`. The free API currently documents a limit of 500 requests
per day per IP; check the provider's current plan and privacy terms before use.

## Configure Telegram SOS

1. Create a Telegram bot using BotFather and copy its bot token.
2. Start a conversation with the bot, or add it to the group that should receive alerts.
3. Obtain the destination chat ID.
4. Put both values in `.env`:

   - `TELEGRAM_BOT_TOKEN`: the token from BotFather
   - `TELEGRAM_CHAT_ID`: the destination chat ID

An SOS sends the captured photo with a timestamped device caption. The Telegram status
pill indicates whether both settings are present. `.env` is ignored by Git; do not commit
credentials to the repository. Values exported in the shell take precedence over `.env`.

Start the server after filling in `.env`:

```bash
python server.py
```

Confirm the cap's `SERVER_URL` in `suneo.ino` uses the current PC LAN IP printed
when the server starts. The address can change when Wi-Fi reconnects; reserve the
PC's address in the router or update and reflash the cap when it changes.

The backend does not perform text-to-speech. A multipart `POST /capture` (such as a
Flutter photo upload) returns the completed result, including its `text`, in the response.
The cap's raw JPEG request returns `202 Accepted` while processing continues. A Flutter
client can poll `GET /api/results?device_id=cap-01&after_id=<last-result-id>` and speak
each new result's `text` on the phone. The endpoint returns only result text and metadata,
not captured image URLs. LAN polling is disabled by default; set
`SUNEO_ALLOW_LAN_RESULTS=1` in `.env` to allow it from another device on the same network.
The web page's optional speech is client-side browser speech and does not use server audio.

## Enroll people

Create one folder per person under `known_faces/`, named for the display name. Add a
few clear, front-facing JPG, PNG, BMP, or WebP photos to each folder, with one person
per image. The server trains its local recognizer on startup.

```text
known_faces/
  Asha/
    front.jpg
    outdoor.png
  Ravi/
    photo1.jpg
```

Detected but unmatched faces are reported as unknown. The detector retries with a
180-degree rotation and contrast equalization when the normal image pass finds no face;
clear, front-facing enrollment photos are still important. LBPH confidence is a relative
match score, not a calibrated probability. Adjust `FACE_RECOGNITION_THRESHOLD` in
`server.py` if matches are too strict or too permissive.

## Enroll currency notes

Create a folder for each denomination under `known_currency/` and add a few note photos:

```text
known_currency/
  10/
    front.jpg
    angled.png
  100/
    front.jpg
```

Folder names become the denomination labels. The prototype compares ORB image features
with these reference photos. It works best when the whole note is visible and reference
photos cover both sides and different angles. This visual matcher is not a validated
banknote-authentication system; do not rely on it to verify that notes are genuine.
Restart the server after changing enrollment photos. The Currency status pill shows the
number of denominations loaded.
