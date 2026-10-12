import 'dart:async';

import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/sync_service.dart';

class MyScansScreen extends StatefulWidget {
  final SyncService sync;
  final DiseaseInfo info;
  const MyScansScreen({super.key, required this.sync, required this.info});

  @override
  State<MyScansScreen> createState() => _MyScansScreenState();
}

class _MyScansScreenState extends State<MyScansScreen> {
  List<Map<String, dynamic>>? _scans;
  String? _error;
  StreamSubscription? _sub;

  @override
  void initState() {
    super.initState();
    _load();
    _sub = widget.sync.reviews.listen((scan) {
      if (_scans == null) return;
      setState(() {
        final i = _scans!.indexWhere((s) => s['id'] == scan['id']);
        if (i >= 0) _scans![i] = scan;
      });
    });
  }

  @override
  void dispose() {
    _sub?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final s = await widget.sync.fetchMyScans();
      if (mounted) setState(() {
        _scans = s;
        _error = null;
      });
    } catch (e) {
      if (mounted) setState(() => _error = 'Could not reach the server. Pull down to try again.\n$e');
    }
  }

  @override
  Widget build(BuildContext context) {
    final server = widget.sync.settings.serverUrl;
    return Scaffold(
      appBar: AppBar(title: const Text('My scans / నా స్కాన్లు')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _error != null
            ? ListView(padding: const EdgeInsets.all(24), children: [Text(_error!)])
            : _scans == null
                ? const Center(child: CircularProgressIndicator())
                : _scans!.isEmpty
                    ? ListView(padding: const EdgeInsets.all(24), children: const [Text('No uploaded scans yet. Scan a leaf from the home screen.')])
                    : ListView.separated(
                        itemCount: _scans!.length,
                        separatorBuilder: (_, __) => const Divider(height: 1),
                        itemBuilder: (_, i) {
                          final s = _scans![i];
                          final r = s['result'] as Map<String, dynamic>;
                          final verified = s['status'] == 'verified';
                          return ListTile(
                            leading: ClipRRect(
                              borderRadius: BorderRadius.circular(6),
                              child: Image.network('$server${s['image_url']}', width: 56, height: 56, fit: BoxFit.cover,
                                  headers: {'X-Api-Key': widget.sync.settings.apiKey},
                                  errorBuilder: (_, __, ___) => const SizedBox(width: 56, height: 56, child: Icon(Icons.image_not_supported_outlined))),
                            ),
                            title: Text('${r['name_te']}  (${r['name_en']})'),
                            subtitle: Text([
                              widget.info.cropNameEn(s['crop']),
                              verified ? 'Checked by ${s['reviewer']}' : (s['status'] == 'auto_healthy' ? 'Healthy (auto)' : 'Waiting for agronomist'),
                              if ((s['review_note'] ?? '').toString().isNotEmpty) 'Note: ${s['review_note']}',
                            ].join('\n')),
                            isThreeLine: true,
                            trailing: Icon(verified ? Icons.verified_outlined : Icons.schedule,
                                color: verified ? Colors.green.shade700 : Colors.orange.shade800),
                            onTap: () => showModalBottomSheet(
                              context: context,
                              builder: (_) => Padding(
                                padding: const EdgeInsets.all(20),
                                child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                                  Text(r['name_te'], style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600)),
                                  const SizedBox(height: 8),
                                  Text(r['advice_te'], style: const TextStyle(fontSize: 17, height: 1.5)),
                                  const SizedBox(height: 8),
                                  Text(r['advice_en']),
                                ]),
                              ),
                            ),
                          );
                        },
                      ),
      ),
    );
  }
}
