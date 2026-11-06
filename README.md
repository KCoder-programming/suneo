# SUNEO

SUNEO (Smart User Navigation & Ear Output) is an accessibility prototype that
combines an ESP32-CAM smart cap, an Android app, and a Python server.

## Project structure

- `backend/` — Flask server, web interface, and local recognition samples
- `flutter/` — Android app
- `SUNEO_ESP32_CAM/` — ESP32-CAM firmware

## Features

The cap and app use the same actions:

- **One press:** scene description, with local face and currency recognition
- **Two presses:** text reading with OCR, plus local face and currency recognition
- **Three or more presses:** SOS photo alert through Telegram

Scene descriptions use Groq. Text reading uses OCR.space. Both are optional
services configured with API keys in the backend environment file.

## Run the server

From the project root, create and activate a virtual environment, then install
the dependencies:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

On Windows, activate with `.venv\Scripts\activate` instead.

Create `backend/.env` from the example and add credentials for the services you
want to use:

```sh
cp backend/.env.example backend/.env
```

Start the server:

```sh
python backend/server.py
```

It serves the web interface at `http://localhost:5000`. Groq, OCR.space, and
Telegram credentials are optional, but the corresponding features will not work
without them.

## Run the Android app

With the server running, start the app:

```sh
cd flutter
flutter pub get
flutter run
```

The Android emulator uses `http://10.0.2.2:5000` by default. On a physical
phone, set the server address in the app to `http://<computer-LAN-IP>:5000`.
Keep the phone and computer on the same network.

## Set up the ESP32-CAM

1. Copy `SUNEO_ESP32_CAM/secrets.h.example` to `SUNEO_ESP32_CAM/secrets.h`.
2. Set your Wi-Fi name and password, and the server URL ending in `/capture`.
3. In Arduino IDE, select the AI Thinker ESP32-CAM board and enable PSRAM.
4. Install the Pololu VL53L0X library, then build and upload the sketch.

The firmware sketch documents the button, buzzer, and distance-sensor wiring.
Use the computer's LAN address for the server URL; `localhost` will not work
from the cap.

## Privacy and security

Photos are processed locally for face and currency recognition. Scene photos
are sent to Groq, OCR photos to OCR.space, and SOS photos to Telegram when those
features are used. The Android app can also send location when permission is
granted. Review each provider's privacy terms before use.

This is a prototype, not a production service. Do not expose the server to the
public internet. Keep API keys, Wi-Fi credentials, and other secrets out of
commits; use the provided example files for configuration.

Built by Team Alpha Force for Google GDG Udaipur's Aravalli Build Championship.

## License

No license.
