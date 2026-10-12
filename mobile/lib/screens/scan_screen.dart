import 'dart:io';

import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:image_picker/image_picker.dart';
import 'package:uuid/uuid.dart';

import '../services/app_state.dart';
import '../services/classifier.dart';
import '../services/sync_service.dart';

const confidenceThreshold = 0.70; // keep in sync with the backend CONFIDENCE_THRESHOLD

class ScanScreen extends StatefulWidget {
  final String crop;
  final DiseaseInfo info;
  final CropClassifier classifier;
  final SyncService sync;
  const ScanScreen({super.key, required this.crop, required this.info, required this.classifier, required this.sync});

  @override
  State<ScanScreen> createState() => _ScanScreenState();
}

class _ScanScreenState extends State<ScanScreen> {
  File? _photo;
  List<Prediction>? _preds;
  bool _noModel = false, _busy = false, _saved = false;
  Position? _pos;
  String? _gpsNote;
  final _farmer = TextEditingController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _takePhoto());
  }

  Future<Position?> _location() async {
    try {
      if (!await Geolocator.isLocationServiceEnabled()) {
        _gpsNote = 'GPS is off. Turn on location so the photo is tagged.';
        return null;
      }
      var perm = await Geolocator.checkPermission();
      if (perm == LocationPermission.denied) perm = await Geolocator.requestPermission();
      if (perm == LocationPermission.denied || perm == LocationPermission.deniedForever) {
        _gpsNote = 'Location permission is off, so this photo has no GPS tag.';
        return null;
      }
      return await Geolocator.getCurrentPosition().timeout(const Duration(seconds: 12));
    } catch (_) {
      _gpsNote = 'Could not get GPS in time. The photo will be saved without it.';
      return null;
    }
  }

  Future<void> _takePhoto() async {
    final shot = await ImagePicker().pickImage(source: ImageSource.camera, maxWidth: 1600, imageQuality: 88);
    if (shot == null) {
      if (_photo == null && mounted) Navigator.pop(context);
      return;
    }
    setState(() {
      _busy = true;
      _photo = File(shot.path);
      _preds = null;
      _saved = false;
      _gpsNote = null;
    });
    final results = await Future.wait<Object?>([
      widget.classifier.classify(widget.crop, File(shot.path)),
      _location(),
    ]);
    if (!mounted) return;
    setState(() {
      _preds = results[0] as List<Prediction>?;
      _noModel = _preds == null;
      _pos = results[1] as Position?;
      _busy = false;
    });
  }

  Future<void> _save() async {
    setState(() => _busy = true);
    final top = _preds?.first;
    await widget.sync.enqueue(
      PendingScan(
        clientId: const Uuid().v4(),
        crop: widget.crop,
        imagePath: _photo!.path,
        capturedAt: DateTime.now().toUtc().toIso8601String(),
        farmerId: _farmer.text.trim(),
        deviceLabel: top?.label,
        deviceConfidence: top?.confidence,
        latitude: _pos?.latitude,
        longitude: _pos?.longitude,
      ),
      _photo!,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      _saved = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    final top = _preds?.first;
    final sure = top != null && top.confidence >= confidenceThreshold;
    final text = widget.info.lookup(widget.crop, sure ? top.label : null);
    final healthy = sure && top.label == 'healthy';
    final colors = Theme.of(context).colorScheme;

    return Scaffold(
      appBar: AppBar(title: Text('${widget.info.cropNameEn(widget.crop)} / ${widget.info.cropNameTe(widget.crop)}')),
      body: _photo == null
          ? const Center(child: CircularProgressIndicator())
          : ListView(padding: const EdgeInsets.all(16), children: [
              ClipRRect(borderRadius: BorderRadius.circular(12), child: Image.file(_photo!, height: 280, fit: BoxFit.cover)),
              const SizedBox(height: 16),
              if (_busy && !_saved) const Center(child: CircularProgressIndicator()),
              if (!_busy && _noModel)
                _Box(
                  color: colors.surfaceContainerHighest,
                  child: const Text('No model for this crop yet. Save the photo and the agronomist will check it. '
                      'Your photos also help train the model.\n\nఈ పంటకు ఇంకా మోడల్ లేదు. ఫోటో సేవ్ చేయండి, నిపుణులు పరిశీలిస్తారు.'),
                ),
              if (!_busy && top != null) ...[
                _Box(
                  color: healthy ? const Color(0xFFE3EEDC) : (sure ? const Color(0xFFF7E1DC) : const Color(0xFFF8EBD7)),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(text.nameEn, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600)),
                    Text(text.nameTe, style: const TextStyle(fontSize: 20)),
                    const SizedBox(height: 4),
                    Text(sure ? 'Confidence ${(top.confidence * 100).round()}%' : 'Low confidence, agronomist will confirm'),
                    const Divider(height: 24),
                    Text(text.adviceTe, style: const TextStyle(fontSize: 17, height: 1.5)),
                    const SizedBox(height: 8),
                    Text(text.adviceEn, style: TextStyle(color: colors.onSurfaceVariant)),
                  ]),
                ),
                const SizedBox(height: 8),
                for (final p in _preds!)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Row(children: [
                      Expanded(child: Text(widget.info.lookup(widget.crop, p.label).nameEn)),
                      SizedBox(width: 120, child: LinearProgressIndicator(value: p.confidence, minHeight: 8)),
                      SizedBox(width: 48, child: Text(' ${(p.confidence * 100).round()}%')),
                    ]),
                  ),
              ],
              if (_gpsNote != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_gpsNote!, style: TextStyle(color: colors.error))),
              if (_pos != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text('GPS ${_pos!.latitude.toStringAsFixed(5)}, ${_pos!.longitude.toStringAsFixed(5)}'),
                ),
              const SizedBox(height: 16),
              TextField(
                controller: _farmer,
                enabled: !_saved,
                decoration: const InputDecoration(labelText: 'Farmer ID (optional) / రైతు ID', border: OutlineInputBorder()),
              ),
              const SizedBox(height: 16),
              if (_saved)
                _Box(
                  color: const Color(0xFFE3EEDC),
                  child: const Text('Saved. It uploads automatically when there is network, and the agronomist\'s answer comes back to this phone.\n'
                      'సేవ్ అయింది. నెట్‌వర్క్ ఉన్నప్పుడు ఆటోమేటిక్‌గా అప్‌లోడ్ అవుతుంది.'),
                )
              else
                FilledButton.icon(
                  onPressed: _busy ? null : _save,
                  icon: const Icon(Icons.cloud_upload_outlined),
                  label: const Text('Save and send / సేవ్ చేయండి'),
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52)),
                ),
              const SizedBox(height: 8),
              OutlinedButton.icon(
                onPressed: _busy ? null : _takePhoto,
                icon: const Icon(Icons.photo_camera_outlined),
                label: Text(_saved ? 'Scan another leaf / ఇంకో ఆకు' : 'Retake photo / మళ్ళీ తీయండి'),
                style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(52)),
              ),
            ]),
    );
  }
}

class _Box extends StatelessWidget {
  final Color color;
  final Widget child;
  const _Box({required this.color, required this.child});
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(12)),
        child: child,
      );
}
