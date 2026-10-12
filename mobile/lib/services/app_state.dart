import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Server address, API key and employee ID, saved on the phone.
class Settings {
  String serverUrl;
  String apiKey;
  String employeeId;

  Settings({required this.serverUrl, required this.apiKey, required this.employeeId});

  bool get isComplete => serverUrl.isNotEmpty && employeeId.isNotEmpty;

  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance();
    return Settings(
      serverUrl: p.getString('serverUrl') ?? '',
      apiKey: p.getString('apiKey') ?? '',
      employeeId: p.getString('employeeId') ?? '',
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('serverUrl', serverUrl.trim().replaceAll(RegExp(r'/+$'), ''));
    await p.setString('apiKey', apiKey.trim());
    await p.setString('employeeId', employeeId.trim());
  }
}

class DiseaseText {
  final String nameEn, nameTe, adviceEn, adviceTe;
  const DiseaseText(this.nameEn, this.nameTe, this.adviceEn, this.adviceTe);
}

/// Same disease_info.json the backend uses, bundled so advice works offline.
class DiseaseInfo {
  final Map<String, dynamic> _data;
  DiseaseInfo._(this._data);

  static Future<DiseaseInfo> load() async =>
      DiseaseInfo._(jsonDecode(await rootBundle.loadString('assets/disease_info.json')));

  Map<String, dynamic> get crops => _data['crops'];

  String cropNameEn(String crop) => crops[crop]['name_en'];
  String cropNameTe(String crop) => crops[crop]['name_te'];

  DiseaseText lookup(String crop, String? label) {
    final cls = label == null ? null : crops[crop]?['classes']?[label];
    final group = cls?['group'] ?? 'uncertain';
    final adv = _data['advice'][group];
    return DiseaseText(
      cls?['name_en'] ?? 'Not sure',
      cls?['name_te'] ?? 'సరిగా గుర్తించలేదు',
      adv['en'],
      adv['te'],
    );
  }
}
