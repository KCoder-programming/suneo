import 'dart:async';
import 'dart:convert';

import 'package:camera/camera.dart';
import 'package:flutter/material.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:speech_to_text/speech_to_text.dart';

void main() {
  runApp(const NetraApp());
}

class NetraApp extends StatelessWidget {
  const NetraApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'SUNEO',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF101A20),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF54D6B0),
          brightness: Brightness.dark,
          surface: const Color(0xFF101A20),
        ),
        useMaterial3: true,
      ),
      home: const NetraHomePage(),
    );
  }
}

class NetraHomePage extends StatefulWidget {
  const NetraHomePage({super.key});

  @override
  State<NetraHomePage> createState() => _NetraHomePageState();
}

class _NetraHomePageState extends State<NetraHomePage> {
  static const _clickWindow = Duration(milliseconds: 450);
  static const _defaultServerUrl = 'http://10.0.2.2:5000';
  static const _serverUrlKey = 'server_url';

  final SpeechToText _speech = SpeechToText();
  final FlutterTts _textToSpeech = FlutterTts();
  final TextEditingController _serverUrlController =
      TextEditingController(text: _defaultServerUrl);
  CameraController? _camera;
  Timer? _releaseTimer;
  Future<void>? _captureSetup;
  Future<Position?>? _locationRequest;
  XFile? _image;
  String _serverUrl = _defaultServerUrl;
  String _transcript = '';
  String _language = 'en';
  String _status = 'Ready';
  String _result = 'Hold the button, speak, then release to send.';
  String? _locationNote;
  int _clickCount = 0;
  bool _sessionActive = false;
  bool _buttonDown = false;
  bool _finishing = false;

  @override
  void initState() {
    super.initState();
    _loadSavedServerUrl();
  }

  @override
  void dispose() {
    _releaseTimer?.cancel();
    if (_speech.isListening) {
      unawaited(_speech.cancel());
    }
    unawaited(_textToSpeech.stop());
    final camera = _camera;
    if (camera != null) {
      unawaited(camera.dispose());
    }
    _serverUrlController.dispose();
    super.dispose();
  }

  Future<void> _loadSavedServerUrl() async {
    final preferences = await SharedPreferences.getInstance();
    final savedUrl = preferences.getString(_serverUrlKey);
    if (savedUrl != null && mounted) {
      setState(() {
        _serverUrl = savedUrl;
        _serverUrlController.text = savedUrl;
      });
    }
  }

  void _pressDown() {
    if (_finishing) return;
    _releaseTimer?.cancel();
    _releaseTimer = null;

    if (!_sessionActive) {
      _sessionActive = true;
      _clickCount = 0;
      _transcript = '';
      _image = null;
      _locationNote = null;
      _captureSetup = _prepareCapture();
      setState(() {
        _status = 'Starting camera and microphone…';
        _result = 'Keep holding and say what you need.';
      });
    }

    _clickCount++;
    setState(() => _buttonDown = true);
  }

  void _pressUp() {
    if (!_sessionActive || !_buttonDown) return;
    setState(() => _buttonDown = false);
    _releaseTimer = Timer(_clickWindow, _finishCapture);
  }

  Future<void> _prepareCapture() async {
    final permissions = await <Permission>[
      Permission.camera,
      Permission.microphone,
      Permission.locationWhenInUse,
    ].request();

    if (permissions[Permission.camera]?.isGranted != true ||
        permissions[Permission.microphone]?.isGranted != true) {
      throw StateError(
        'Allow camera and microphone access in Android settings to use SUNEO.',
      );
    }

    if (permissions[Permission.locationWhenInUse]?.isGranted == true) {
      _locationRequest = _getCurrentLocation();
    } else {
      _locationNote = 'Location permission was not granted.';
    }

    await Future.wait([_initializeCamera(), _startSpeechRecognition()]);
    final image = await _camera!.takePicture();
    _image = image;
    if (mounted) {
      setState(() {
        _status = _speech.isListening ? 'Listening' : 'Camera ready';
      });
    }
  }

  Future<void> _initializeCamera() async {
    if (_camera?.value.isInitialized == true) return;

    final cameras = await availableCameras();
    if (cameras.isEmpty) {
      throw StateError('No camera is available on this device.');
    }
    final backCamera = cameras.where(
      (camera) => camera.lensDirection == CameraLensDirection.back,
    );
    final controller = CameraController(
      backCamera.isNotEmpty ? backCamera.first : cameras.first,
      ResolutionPreset.medium,
      enableAudio: false,
      imageFormatGroup: ImageFormatGroup.jpeg,
    );
    _camera = controller;
    await controller.initialize();
  }

  Future<void> _startSpeechRecognition() async {
    final available = await _speech.initialize(
      onStatus: (status) {
        if (mounted && _sessionActive) {
          setState(() {
            if (status == 'listening') _status = 'Listening';
          });
        }
      },
      onError: (error) {
        if (mounted && _sessionActive) {
          setState(() => _status = 'Speech recognition: ${error.errorMsg}');
        }
      },
    );
    if (!available) {
      throw StateError(
        'Speech recognition is unavailable. Check that Android speech services are enabled.',
      );
    }

    final systemLocale = await _speech.systemLocale();
    final localeId = systemLocale?.localeId;
    _language = localeId?.toLowerCase().startsWith('hi') == true ? 'hi' : 'en';
    await _speech.listen(
      onResult: (result) {
        if (mounted && (_sessionActive || _finishing)) {
          setState(() => _transcript = result.recognizedWords);
        }
      },
      listenOptions: SpeechListenOptions(
        localeId: localeId,
        listenMode: ListenMode.dictation,
        partialResults: true,
        cancelOnError: false,
        listenFor: const Duration(minutes: 5),
        pauseFor: const Duration(seconds: 30),
      ),
    );
  }

  Future<Position?> _getCurrentLocation() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        _locationNote = 'Location services are turned off.';
        return null;
      }
      return await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.high,
          timeLimit: Duration(seconds: 5),
        ),
      );
    } on Exception catch (error) {
      _locationNote = 'Location unavailable: $error';
      return null;
    }
  }

  Future<void> _finishCapture() async {
    if (!_sessionActive || _finishing) return;
    _sessionActive = false;
    _finishing = true;
    final clickCount = _clickCount;
    final clickType = clickCount == 1
        ? 'scene'
        : clickCount == 2
            ? 'ocr'
            : 'sos';

    setState(() {
      _buttonDown = false;
      _status = 'Preparing upload…';
    });

    try {
      await _captureSetup;
      if (_speech.isListening) await _speech.stop();
      final image = _image;
      if (image == null) {
        throw StateError('The camera did not produce an image.');
      }

      final position = await _locationRequest;
      await _sendCapture(image, clickType, position);
    } catch (error) {
      if (_speech.isListening) await _speech.cancel();
      if (mounted) {
        setState(() {
          _status = 'Capture failed';
          _result = error.toString().replaceFirst('Bad state: ', '');
        });
      }
    } finally {
      _finishing = false;
      _clickCount = 0;
      _image = null;
      _captureSetup = null;
      _locationRequest = null;
    }
  }

  Future<void> _sendCapture(
    XFile image,
    String clickType,
    Position? position,
  ) async {
    final baseUri = Uri.tryParse(_serverUrl);
    if (baseUri == null ||
        !{'http', 'https'}.contains(baseUri.scheme) ||
        baseUri.host.isEmpty) {
      throw const FormatException('Set a valid server address first.');
    }

    final uri = baseUri.replace(
      path: '${baseUri.path.replaceFirst(RegExp(r'/$'), '')}/capture',
      query: null,
      fragment: null,
    );
    final request = http.MultipartRequest('POST', uri)
      ..fields['click_type'] = clickType
      ..fields['device_id'] = 'netra-android'
      ..fields['focus'] = _transcript
      ..fields['lang'] = _speechLanguage;

    if (position != null) {
      request.fields['lat'] = position.latitude.toString();
      request.fields['lng'] = position.longitude.toString();
    }
    request.files.add(
      http.MultipartFile.fromBytes(
        'image',
        await image.readAsBytes(),
        filename: 'capture.jpg',
      ),
    );

    if (mounted) {
      setState(() => _status = 'Sending ${clickType.toUpperCase()}…');
    }
    final streamedResponse = await request.send().timeout(
          const Duration(seconds: 45),
        );
    final response = await http.Response.fromStream(streamedResponse);
    final decoded = _decodeResponse(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final error = decoded?['text']?.toString() ??
          decoded?['error']?.toString() ??
          response.body;
      throw StateError('Server returned ${response.statusCode}: $error');
    }
    final serverText = decoded?['text']?.toString().trim();
    if (serverText == null || serverText.isEmpty) {
      throw StateError('Server response did not contain a result to read.');
    }

    final transcript =
        _transcript.isEmpty ? 'No speech recognized.' : _transcript;
    if (mounted) {
      setState(() {
        _status = 'Result received · ${clickType.toUpperCase()}';
        _result = [
          'You said: $transcript',
          'Server: $serverText',
          if (_locationNote != null) _locationNote!,
        ].join('\n\n');
      });
    }
    await _speakResponse(serverText);
  }

  Future<void> _speakResponse(String text) async {
    try {
      await _textToSpeech.awaitSpeakCompletion(true);
      final languageResult = await _textToSpeech.setLanguage(
        _speechLanguage == 'hi' ? 'hi-IN' : 'en-US',
      );
      if (languageResult == 0) {
        throw StateError(
          'The device does not have a voice installed for $_speechLanguage.',
        );
      }
      await _textToSpeech.setSpeechRate(0.48);
      final speakResult = await _textToSpeech.speak(text);
      if (speakResult == 0) {
        throw StateError('Android could not start speech playback.');
      }
      if (mounted) setState(() => _status = 'Response spoken');
    } on Exception catch (error) {
      if (mounted) {
        setState(() {
          _status = 'Speech playback failed';
          _result = '$_result\n\nAudio playback failed: $error';
        });
      }
    }
  }

  String get _speechLanguage {
    return _language;
  }

  Map<String, dynamic>? _decodeResponse(String body) {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      return null;
    }
  }

  Future<void> _saveServerUrl() async {
    final serverUrl = _serverUrlController.text.trim();
    if (serverUrl.isEmpty) return;
    final uri = Uri.tryParse(serverUrl);
    if (uri == null ||
        !{'http', 'https'}.contains(uri.scheme) ||
        uri.host.isEmpty) {
      setState(() {
        _status = 'Invalid server address';
        _result = 'Enter a complete address, such as http://192.168.1.10:5000';
      });
      return;
    }

    final preferences = await SharedPreferences.getInstance();
    await preferences.setString(_serverUrlKey, serverUrl);
    if (mounted) {
      setState(() {
        _serverUrl = serverUrl;
        _status = 'Ready';
        _result = 'Server address saved.';
      });
    }
  }

  Future<void> _accessibleCapture() async {
    _pressDown();
    _pressUp();
  }

  @override
  Widget build(BuildContext context) {
    final isActive = _buttonDown || _sessionActive;
    return Scaffold(
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            final diameter = (constraints.maxHeight * 0.48).clamp(240.0, 340.0);
            return Padding(
              padding: const EdgeInsets.fromLTRB(24, 20, 24, 18),
              child: Column(
                children: [
                  Row(
                    children: [
                      Container(
                        width: 42,
                        height: 42,
                        decoration: BoxDecoration(
                          color: const Color(0xFF1E302F),
                          borderRadius: BorderRadius.circular(14),
                        ),
                        child: const Icon(
                          Icons.remove_red_eye_outlined,
                          color: Color(0xFF77E7C4),
                        ),
                      ),
                      const SizedBox(width: 12),
                      const Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              'SUNEO',
                              style: TextStyle(
                                letterSpacing: 2.8,
                                fontWeight: FontWeight.w800,
                                fontSize: 17,
                              ),
                            ),
                            Text(
                              'Your voice-first companion',
                              style: TextStyle(
                                color: Color(0xFF9AA9AD),
                                fontSize: 12,
                              ),
                            ),
                          ],
                        ),
                      ),
                      const Icon(Icons.circle,
                          size: 9, color: Color(0xFF77E7C4)),
                      const SizedBox(width: 7),
                      Text(
                        _status,
                        style: const TextStyle(
                          color: Color(0xFFB7C7C8),
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                  const Spacer(),
                  Text(
                    isActive ? 'I’M LISTENING' : 'READY WHEN YOU ARE',
                    style: const TextStyle(
                      color: Color(0xFF9AA9AD),
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 2.1,
                    ),
                  ),
                  const SizedBox(height: 24),
                  Semantics(
                    button: true,
                    label: 'Hold to capture and speak',
                    hint:
                        'One press describes the scene. Two presses read text. Three presses send an SOS.',
                    onTap: _accessibleCapture,
                    child: GestureDetector(
                      behavior: HitTestBehavior.opaque,
                      onTapDown: (_) => _pressDown(),
                      onTapUp: (_) => _pressUp(),
                      onTapCancel: _pressUp,
                      child: AnimatedContainer(
                        duration: const Duration(milliseconds: 160),
                        width: diameter,
                        height: diameter,
                        transform: Matrix4.diagonal3Values(
                          _buttonDown ? 0.96 : 1,
                          _buttonDown ? 0.96 : 1,
                          1,
                        ),
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: _buttonDown
                              ? const Color(0xFF276C5A)
                              : const Color(0xFF1C5148),
                          border: Border.all(
                            color: const Color(0xFF5FD5B1),
                            width: 2,
                          ),
                          boxShadow: [
                            BoxShadow(
                              color: const Color(0xFF3BC49A).withValues(
                                alpha: _buttonDown ? 0.34 : 0.17,
                              ),
                              blurRadius: _buttonDown ? 52 : 34,
                              spreadRadius: _buttonDown ? 8 : 2,
                            ),
                          ],
                        ),
                        child: Column(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Icon(
                              _buttonDown ? Icons.graphic_eq : Icons.mic_none,
                              size: 58,
                              color: const Color(0xFFB8F9E4),
                            ),
                            const SizedBox(height: 18),
                            Text(
                              _buttonDown ? 'LISTENING' : 'HOLD TO TALK',
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 16,
                                fontWeight: FontWeight.w800,
                                letterSpacing: 1.8,
                              ),
                            ),
                            const SizedBox(height: 9),
                            const Text(
                              'release to send',
                              style: TextStyle(
                                color: Color(0xFFB8D9D0),
                                fontSize: 13,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 30),
                  Text(
                    _transcript.isEmpty
                        ? 'Hold, speak, release'
                        : '“$_transcript”',
                    textAlign: TextAlign.center,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Color(0xFFD4E3E1),
                      fontSize: 16,
                      height: 1.4,
                    ),
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    '1× SCENE     ·     2× READ TEXT     ·     3× SOS',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Color(0xFF84969A),
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 0.7,
                    ),
                  ),
                  const Spacer(),
                  if (_result.isNotEmpty)
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 12,
                      ),
                      decoration: BoxDecoration(
                        color: const Color(0xFF18252B),
                        borderRadius: BorderRadius.circular(16),
                      ),
                      child: Text(
                        _result,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          color: Color(0xFFB7C7C8),
                          fontSize: 12,
                          height: 1.4,
                        ),
                      ),
                    ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: SizedBox(
                          height: 44,
                          child: TextField(
                            controller: _serverUrlController,
                            enabled: !_finishing && !_sessionActive,
                            keyboardType: TextInputType.url,
                            textInputAction: TextInputAction.done,
                            autocorrect: false,
                            onSubmitted: (_) => _saveServerUrl(),
                            style: const TextStyle(fontSize: 12),
                            decoration: InputDecoration(
                              hintText: 'http://192.168.1.10:5000',
                              prefixIcon: const Icon(
                                Icons.dns_outlined,
                                size: 18,
                              ),
                              contentPadding: const EdgeInsets.symmetric(
                                horizontal: 12,
                                vertical: 8,
                              ),
                              filled: true,
                              fillColor: const Color(0xFF18252B),
                              border: OutlineInputBorder(
                                borderRadius: BorderRadius.circular(12),
                                borderSide: BorderSide.none,
                              ),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      SizedBox(
                        height: 44,
                        child: FilledButton(
                          onPressed: _finishing || _sessionActive
                              ? null
                              : _saveServerUrl,
                          style: FilledButton.styleFrom(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 14,
                            ),
                          ),
                          child: const Text('Save'),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            );
          },
        ),
      ),
    );
  }
}
