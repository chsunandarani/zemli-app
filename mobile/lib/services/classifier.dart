import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/services.dart';
import 'package:image/image.dart' as img;
import 'package:tflite_flutter/tflite_flutter.dart';

class Prediction {
  final String label;
  final double confidence;
  const Prediction(this.label, this.confidence);
}

/// Runs the crop's model on the phone, so a result appears even with no network.
/// Preprocessing matches training: short side 256, center crop 224, ImageNet normalisation.
/// The TFLite model from onnx2tf takes NHWC input [1, 224, 224, 3].
class CropClassifier {
  static const _size = 224;
  static const _mean = [0.485, 0.456, 0.406];
  static const _std = [0.229, 0.224, 0.225];

  final Map<String, Interpreter> _interpreters = {};
  final Map<String, List<String>> _labels = {};
  final Set<String> _missing = {};

  /// Returns false if no model is bundled for this crop (scan still gets saved for review).
  Future<bool> ensureLoaded(String crop) async {
    if (_interpreters.containsKey(crop)) return true;
    if (_missing.contains(crop)) return false;
    try {
      final labelsText = await rootBundle.loadString('assets/models/${crop}_labels.txt');
      _labels[crop] = labelsText.split('\n').map((l) => l.trim()).where((l) => l.isNotEmpty).toList();
      _interpreters[crop] = await Interpreter.fromAsset('assets/models/$crop.tflite');
      return true;
    } catch (_) {
      _missing.add(crop);
      return false;
    }
  }

  Future<List<Prediction>?> classify(String crop, File imageFile) async {
    if (!await ensureLoaded(crop)) return null;
    final interpreter = _interpreters[crop]!;
    final labels = _labels[crop]!;

    final decoded = img.decodeImage(await imageFile.readAsBytes());
    if (decoded == null) return null;
    var image = img.bakeOrientation(decoded);
    final scale = 256 / math.min(image.width, image.height);
    image = img.copyResize(image,
        width: math.max(_size, (image.width * scale).round()),
        height: math.max(_size, (image.height * scale).round()),
        interpolation: img.Interpolation.linear);
    image = img.copyCrop(image,
        x: (image.width - _size) ~/ 2, y: (image.height - _size) ~/ 2, width: _size, height: _size);

    final input = List.generate(
      1,
      (_) => List.generate(
        _size,
        (y) => List.generate(_size, (x) {
          final p = image.getPixel(x, y);
          return [
            (p.r / 255.0 - _mean[0]) / _std[0],
            (p.g / 255.0 - _mean[1]) / _std[1],
            (p.b / 255.0 - _mean[2]) / _std[2],
          ];
        }),
      ),
    );
    final output = [List<double>.filled(labels.length, 0.0)];
    interpreter.run(input, output);

    final logits = output[0];
    final maxLogit = logits.reduce(math.max);
    final exps = logits.map((v) => math.exp(v - maxLogit)).toList();
    final sum = exps.reduce((a, b) => a + b);
    final preds = [for (var i = 0; i < labels.length; i++) Prediction(labels[i], exps[i] / sum)]
      ..sort((a, b) => b.confidence.compareTo(a.confidence));
    return preds.take(3).toList();
  }
}
