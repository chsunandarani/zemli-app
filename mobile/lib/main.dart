import 'package:flutter/material.dart';

import 'screens/my_scans_screen.dart';
import 'screens/scan_screen.dart';
import 'services/app_state.dart';
import 'services/classifier.dart';
import 'services/sync_service.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final settings = await Settings.load();
  final info = await DiseaseInfo.load();
  final sync = SyncService(settings);
  await sync.start();
  runApp(ZemliApp(settings: settings, info: info, sync: sync));
}

class ZemliApp extends StatelessWidget {
  final Settings settings;
  final DiseaseInfo info;
  final SyncService sync;
  const ZemliApp({super.key, required this.settings, required this.info, required this.sync});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Zemli Field',
        debugShowCheckedModeBanner: false,
        themeMode: ThemeMode.light,
        theme: ThemeData(colorSchemeSeed: const Color(0xFF2E6B30), useMaterial3: true),
        home: HomeScreen(settings: settings, info: info, sync: sync),
      );
}

class HomeScreen extends StatefulWidget {
  final Settings settings;
  final DiseaseInfo info;
  final SyncService sync;
  const HomeScreen({super.key, required this.settings, required this.info, required this.sync});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final classifier = CropClassifier();

  @override
  void initState() {
    super.initState();
    widget.sync.reviews.listen((scan) {
      if (!mounted) return;
      final r = scan['result'];
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Agronomist checked a scan: ${r['name_te']} (${r['name_en']})'),
        action: SnackBarAction(label: 'View', onPressed: _openMyScans),
        duration: const Duration(seconds: 8),
      ));
    });
    if (!widget.settings.isComplete) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _openSettings());
    }
  }

  void _openMyScans() => Navigator.push(
      context, MaterialPageRoute(builder: (_) => MyScansScreen(sync: widget.sync, info: widget.info)));

  Future<void> _openSettings() async {
    final s = widget.settings;
    final url = TextEditingController(text: s.serverUrl);
    final key = TextEditingController(text: s.apiKey);
    final emp = TextEditingController(text: s.employeeId);
    final saved = await showDialog<bool>(
      context: context,
      barrierDismissible: s.isComplete,
      builder: (ctx) => AlertDialog(
        title: const Text('Setup'),
        content: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: url, decoration: const InputDecoration(labelText: 'Server address', hintText: 'https://your-server.com'), keyboardType: TextInputType.url),
            TextField(controller: key, decoration: const InputDecoration(labelText: 'API key'), obscureText: true),
            TextField(controller: emp, decoration: const InputDecoration(labelText: 'Your employee ID', hintText: 'VE-429')),
          ]),
        ),
        actions: [FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Save'))],
      ),
    );
    if (saved == true) {
      s
        ..serverUrl = url.text.trim().replaceAll(RegExp(r'/+$'), '')
        ..apiKey = key.text.trim()
        ..employeeId = emp.text.trim();
      await s.save();
      widget.sync.restart();
      setState(() {});
    }
  }

  @override
  Widget build(BuildContext context) {
    final crops = widget.info.crops.keys.toList();
    return Scaffold(
      appBar: AppBar(
        title: const Text('Zemli Field'),
        actions: [IconButton(onPressed: _openSettings, icon: const Icon(Icons.settings_outlined), tooltip: 'Setup')],
      ),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Text('Which crop are you checking?\nఏ పంటను పరిశీలిస్తున్నారు?', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 12),
        for (final c in crops)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: FilledButton.tonal(
              style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(76), alignment: Alignment.centerLeft,
                  padding: const EdgeInsets.symmetric(horizontal: 20)),
              onPressed: () => Navigator.push(context, MaterialPageRoute(
                  builder: (_) => ScanScreen(crop: c, info: widget.info, classifier: classifier, sync: widget.sync))),
              child: Row(children: [
                const Icon(Icons.photo_camera_outlined, size: 28),
                const SizedBox(width: 16),
                Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  Text(widget.info.cropNameTe(c), style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600)),
                  Text(widget.info.cropNameEn(c)),
                ]),
              ]),
            ),
          ),
        const SizedBox(height: 8),
        OutlinedButton.icon(
          onPressed: _openMyScans,
          icon: const Icon(Icons.fact_check_outlined),
          label: const Text('My scans and agronomist answers'),
          style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(52)),
        ),
        const SizedBox(height: 20),
        ValueListenableBuilder<int>(
          valueListenable: widget.sync.pendingCount,
          builder: (_, n, __) => ValueListenableBuilder<bool>(
            valueListenable: widget.sync.online,
            builder: (_, on, __) => ListTile(
              leading: Icon(on ? Icons.cloud_done_outlined : Icons.cloud_off_outlined, color: on ? Colors.green.shade700 : Colors.grey),
              title: Text(n == 0 ? 'All scans uploaded' : '$n scan${n == 1 ? '' : 's'} waiting to upload'),
              subtitle: Text(on ? 'Connected to server' : 'Offline. Scans are saved and will upload later.'),
              trailing: n > 0 ? TextButton(onPressed: widget.sync.syncNow, child: const Text('Upload now')) : null,
            ),
          ),
        ),
      ]),
    );
  }
}
