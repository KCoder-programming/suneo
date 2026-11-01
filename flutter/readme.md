# Netra for Android

Netra is a single-button voice and vision companion. Press and hold the large
button to capture a photo and dictate a request. Release to send the photo,
speech transcript, and available location to the Netra server. The app displays
the server's returned text and reads it aloud using Android text-to-speech.

The button follows the Suneo hardware click mapping:

- One press: scene description (`scene`)
- Two presses: local analysis and OCR (`ocr`)
- Three or more presses: SOS (`sos`)

Rapid presses are grouped using the same 450 ms click window as the hardware.
The app requests camera, microphone, and location access when first used.
Camera and microphone access are required; location is attached when available.

## Run on Android

Start `server.py` on the same network as the phone, then set the server address
in the app. For an Android emulator, the default `http://10.0.2.2:5000` routes
to the host computer. On a physical phone, use the computer's LAN address, for
example `http://192.168.1.10:5000`.

```sh
flutter pub get
flutter run
```

The app posts a multipart request to `/capture`, including the image, click
type, device ID, language, location when available, and the speech transcript.
The transcript is sent in the server's existing `focus` field. The backend and
`suneo.ino` are not modified.
