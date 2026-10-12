import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'app_state.dart';

/// One scan waiting to reach the server. Saved to disk so nothing is lost
/// when the phone is offline or the app is closed.
class PendingScan {
  final String clientId, crop, imagePath, capturedAt;
  final String? farmerId, deviceLabel;
  final double? latitude, longitude, deviceConfidence;

  PendingScan({
    required this.clientId,
    required this.crop,
    required this.imagePath,
    required this.capturedAt,
    this.farmerId,
    this.deviceLabel,
    this.deviceConfidence,
    this.latitude,
    this.longitude,
  });

  Map<String, dynamic> toJson() => {
        'clientId': clientId, 'crop': crop, 'imagePath': imagePath, 'capturedAt': capturedAt,
        'farmerId': farmerId, 'deviceLabel': deviceLabel, 'deviceConfidence': deviceConfidence,
        'latitude': latitude, 'longitude': longitude,
      };

  factory PendingScan.fromJson(Map<String, dynamic> j) => PendingScan(
        clientId: j['clientId'], crop: j['crop'], imagePath: j['imagePath'], capturedAt: j['capturedAt'],
        farmerId: j['farmerId'], deviceLabel: j['deviceLabel'],
        deviceConfidence: (j['deviceConfidence'] as num?)?.toDouble(),
        latitude: (j['latitude'] as num?)?.toDouble(), longitude: (j['longitude'] as num?)?.toDouble(),
      );
}

class SyncService {
  final Settings settings;
  final ValueNotifier<int> pendingCount = ValueNotifier(0);
  final ValueNotifier<bool> online = ValueNotifier(false);
  final _reviews = StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get reviews => _reviews.stream;

  final List<PendingScan> _queue = [];
  late Directory _dir;
  Timer? _timer;
  bool _syncing = false;
  WebSocketChannel? _ws;
  Timer? _wsRetry, _wsPing;

  SyncService(this.settings);

  Future<void> start() async {
    _dir = Directory('${(await getApplicationDocumentsDirectory()).path}/zemli_scans');
    await _dir.create(recursive: true);
    final file = File('${_dir.path}/queue.json');
    if (await file.exists()) {
      final list = jsonDecode(await file.readAsString()) as List;
      _queue.addAll(list.map((e) => PendingScan.fromJson(e)));
    }
    pendingCount.value = _queue.length;
    _timer = Timer.periodic(const Duration(seconds: 30), (_) => syncNow());
    syncNow();
    _connectWs();
  }

  void dispose() {
    _timer?.cancel();
    _wsRetry?.cancel();
    _wsPing?.cancel();
    _ws?.sink.close();
  }

  void restart() {
    _ws?.sink.close();
    _connectWs();
    syncNow();
  }

  Future<void> _persist() async {
    await File('${_dir.path}/queue.json').writeAsString(jsonEncode(_queue.map((e) => e.toJson()).toList()));
    pendingCount.value = _queue.length;
  }

  /// Copies the photo into app storage (camera temp files can be deleted) and queues it.
  Future<void> enqueue(PendingScan scan, File photo) async {
    final stored = await photo.copy('${_dir.path}/${scan.clientId}.jpg');
    _queue.add(PendingScan.fromJson({...scan.toJson(), 'imagePath': stored.path}));
    await _persist();
    syncNow();
  }

  Future<void> syncNow() async {
    if (_syncing || !settings.isComplete) return;
    _syncing = true;
    try {
      while (_queue.isNotEmpty) {
        final s = _queue.first;
        final req = http.MultipartRequest('POST', Uri.parse('${settings.serverUrl}/api/scans'))
          ..headers['X-Api-Key'] = settings.apiKey
          ..fields.addAll({
            'client_id': s.clientId,
            'crop': s.crop,
            'employee_id': settings.employeeId,
            'captured_at': s.capturedAt,
            if (s.farmerId != null && s.farmerId!.isNotEmpty) 'farmer_id': s.farmerId!,
            if (s.latitude != null) 'latitude': '${s.latitude}',
            if (s.longitude != null) 'longitude': '${s.longitude}',
            if (s.deviceLabel != null) 'device_label': s.deviceLabel!,
            if (s.deviceConfidence != null) 'device_confidence': '${s.deviceConfidence}',
          })
          ..files.add(await http.MultipartFile.fromPath('image', s.imagePath));
        final resp = await req.send().timeout(const Duration(seconds: 40));
        if (resp.statusCode == 200) {
          _queue.removeAt(0);
          await _persist();
          final f = File(s.imagePath);
          if (await f.exists()) await f.delete();
          online.value = true;
        } else if (resp.statusCode == 400) {
          _queue.removeAt(0); // server can never accept this one (e.g. broken image); don't block the rest
          await _persist();
        } else {
          break; // 401/500 etc: keep it and try later
        }
      }
    } catch (_) {
      online.value = false; // no network; the timer will retry
    } finally {
      _syncing = false;
    }
  }

  Future<List<Map<String, dynamic>>> fetchMyScans() async {
    final uri = Uri.parse('${settings.serverUrl}/api/scans?employee_id=${Uri.encodeQueryComponent(settings.employeeId)}&limit=100');
    final r = await http.get(uri, headers: {'X-Api-Key': settings.apiKey}).timeout(const Duration(seconds: 20));
    if (r.statusCode != 200) throw Exception('Server replied ${r.statusCode}');
    return (jsonDecode(utf8.decode(r.bodyBytes)) as List).cast<Map<String, dynamic>>();
  }

  void _connectWs() {
    _wsRetry?.cancel();
    _wsPing?.cancel();
    if (!settings.isComplete) return;
    final base = settings.serverUrl.replaceFirst(RegExp(r'^http'), 'ws');
    final uri = Uri.parse('$base/ws?role=employee'
        '&employee_id=${Uri.encodeQueryComponent(settings.employeeId)}'
        '&key=${Uri.encodeQueryComponent(settings.apiKey)}');
    try {
      final ch = WebSocketChannel.connect(uri);
      _ws = ch;
      ch.stream.listen(
        (msg) {
          online.value = true;
          final data = jsonDecode(msg as String) as Map<String, dynamic>;
          if (data['type'] == 'scan_reviewed') _reviews.add(data['scan']);
        },
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
        cancelOnError: true,
      );
      _wsPing = Timer.periodic(const Duration(seconds: 25), (_) {
        try {
          ch.sink.add('ping');
        } catch (_) {}
      });
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    online.value = false;
    _wsPing?.cancel();
    _wsRetry?.cancel();
    _wsRetry = Timer(const Duration(seconds: 10), () {
      _connectWs();
      syncNow(); // network may be back, push anything waiting
    });
  }
}
