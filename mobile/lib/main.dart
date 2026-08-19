import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'market_categories_page.dart';

void main() {
  runApp(const JasongApp());
}

class JasongApp extends StatelessWidget {
  const JasongApp({super.key});

  @override
  Widget build(BuildContext context) {
    const teal = Color(0xFF65E6D3);
    return MaterialApp(
      title: 'Jasong AI Trader',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF06131B),
        colorScheme: ColorScheme.fromSeed(
          seedColor: teal,
          brightness: Brightness.dark,
          primary: teal,
          secondary: const Color(0xFF6FA8FF),
          surface: const Color(0xFF0E1A24),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
          scrolledUnderElevation: 0,
        ),
        navigationBarTheme: NavigationBarThemeData(
          backgroundColor: const Color(0xFF0A151E),
          indicatorColor: teal.withValues(alpha: .16),
          labelTextStyle: WidgetStateProperty.resolveWith((states) {
            return TextStyle(
              fontSize: 9.5,
              fontWeight: states.contains(WidgetState.selected)
                  ? FontWeight.w800
                  : FontWeight.w500,
              color: states.contains(WidgetState.selected)
                  ? teal
                  : Colors.white54,
            );
          }),
        ),
      ),
      home: const JasongShell(),
    );
  }
}

class JasongShell extends StatefulWidget {
  const JasongShell({super.key});

  @override
  State<JasongShell> createState() => _JasongShellState();
}

class _JasongShellState extends State<JasongShell>
    with WidgetsBindingObserver {
  static const _teal = Color(0xFF65E6D3);
  static const _green = Color(0xFF67F0C1);
  static const _red = Color(0xFFFF7E8B);
  static const _amber = Color(0xFFFFD75E);
  static const _blue = Color(0xFF6FA8FF);
  static const _purple = Color(0xFFB899FF);

  final String apiBase = const String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'https://jasong-ai-trader-v2.onrender.com',
  );

  final http.Client _client = http.Client();

  int selectedTab = 0;
  bool refreshing = false;
  String? error;
  String? activeEndpoint;
  DateTime? lastUpdated;
  Timer? refreshTimer;

  Map<String, dynamic> portfolioStatus = {};
  Map<String, dynamic> forwardStatus = {};
  Map<String, dynamic> forwardLearning = {};
  Map<String, dynamic> compoundPayload = {};
  Map<String, dynamic> dataHealth = {};
  List<Map<String, dynamic>> categoryPositions = [];
  List<Map<String, dynamic>> forwardTrades = [];

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Future.microtask(() => refreshAll());

    refreshTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) {
        if (selectedTab != 1 && !refreshing) {
          refreshAll(silent: true);
        }
      },
    );
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    refreshTimer?.cancel();
    _client.close();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && selectedTab != 1) {
      refreshAll(silent: true);
    }
  }

  Future<Map<String, dynamic>> _get(
    String path, {
    int timeoutSeconds = 20,
  }) async {
    final response = await _client
        .get(
          Uri.parse('$apiBase$path'),
          headers: const {
            'Accept': 'application/json',
            'Cache-Control': 'no-cache',
          },
        )
        .timeout(Duration(seconds: timeoutSeconds));

    if (response.statusCode != 200) {
      throw HttpException('HTTP ${response.statusCode}');
    }

    final body = response.body.trim();
    if (body.startsWith('<')) {
      throw const FormatException('Backend returned HTML');
    }

    final decoded = jsonDecode(body);
    if (decoded is! Map) {
      throw const FormatException('Expected JSON object');
    }
    return Map<String, dynamic>.from(decoded);
  }

  bool _transient(Object e) {
    final text = e.toString().toLowerCase();
    return e is TimeoutException ||
        e is SocketException ||
        e is http.ClientException ||
        text.contains('502') ||
        text.contains('503') ||
        text.contains('504') ||
        text.contains('timeout') ||
        text.contains('timed out') ||
        text.contains('connection reset') ||
        text.contains('connection closed');
  }

  String _friendlyError(String path, Object e) {
    final text = e.toString().toLowerCase();

    if (text.contains('502')) {
      return '$path: Render is temporarily busy (HTTP 502).';
    }
    if (text.contains('503')) {
      return '$path: backend temporarily unavailable (HTTP 503).';
    }
    if (text.contains('504') || e is TimeoutException) {
      return '$path: request timed out.';
    }
    if (e is SocketException || e is http.ClientException) {
      return '$path: network connection interrupted.';
    }
    if (e is FormatException) {
      return '$path: invalid server response.';
    }
    return '$path: ${e.runtimeType}.';
  }

  Future<Map<String, dynamic>> _getRetry(
    String path, {
    int timeoutSeconds = 20,
    int attempts = 2,
  }) async {
    Object? lastError;

    for (var attempt = 1; attempt <= attempts; attempt++) {
      try {
        return await _get(
          path,
          timeoutSeconds: timeoutSeconds,
        );
      } catch (e) {
        lastError = e;
        if (!_transient(e) || attempt >= attempts) {
          rethrow;
        }
        await Future.delayed(Duration(seconds: attempt * 2));
      }
    }

    throw lastError ?? const HttpException('Unknown request failure');
  }

  void _markSuccess() {
    if (!mounted) return;
    setState(() {
      lastUpdated = DateTime.now();
      error = null;
    });
  }

  Future<void> _loadStep(
    String path,
    void Function(Map<String, dynamic>) apply, {
    int timeoutSeconds = 20,
    bool silent = false,
  }) async {
    if (!mounted) return;

    setState(() {
      activeEndpoint = path;
    });

    try {
      final payload = await _getRetry(
        path,
        timeoutSeconds: timeoutSeconds,
      );

      if (!mounted) return;
      setState(() {
        apply(payload);
        lastUpdated = DateTime.now();
        error = null;
      });
    } catch (e) {
      if (!mounted) return;
      if (!silent) {
        setState(() {
          error = _friendlyError(path, e);
        });
      }
    }
  }

  List<Map<String, dynamic>> _mapList(dynamic value) {
    if (value is! List) return const [];
    return value
        .whereType<Map>()
        .map((row) => Map<String, dynamic>.from(row))
        .toList();
  }

  Future<void> refreshAll({bool silent = false}) async {
    if (refreshing) return;

    if (mounted) {
      setState(() {
        refreshing = true;
        activeEndpoint = 'connecting';
        if (!silent) error = null;
      });
    }

    try {
      // Progressive loading:
      // each successful response updates the screen immediately.
      // Heavy/secondary endpoints cannot hold the whole UI at zero.

      await _loadStep(
        '/category-portfolio/status',
        (payload) => portfolioStatus = payload,
        timeoutSeconds: 15,
        silent: silent,
      );

      await _loadStep(
        '/forward-validation/status',
        (payload) => forwardStatus = payload,
        timeoutSeconds: 20,
        silent: silent,
      );

      await _loadStep(
        '/category-portfolio/positions',
        (payload) => categoryPositions = _mapList(payload['positions']),
        timeoutSeconds: 15,
        silent: silent,
      );

      await _loadStep(
        '/forward-validation/trades',
        (payload) => forwardTrades = _mapList(payload['trades']),
        timeoutSeconds: 20,
        silent: silent,
      );

      await _loadStep(
        '/forward-validation/learning',
        (payload) => forwardLearning = payload,
        timeoutSeconds: 15,
        silent: silent,
      );

      await _loadStep(
        '/market-categories/data-health',
        (payload) => dataHealth = payload,
        timeoutSeconds: 15,
        silent: silent,
      );

      // Compound candidate generation can be heavier, so load it last.
      await _loadStep(
        '/market-categories/compound-candidates',
        (payload) => compoundPayload = payload,
        timeoutSeconds: 30,
        silent: silent,
      );
    } finally {
      if (mounted) {
        setState(() {
          refreshing = false;
          activeEndpoint = null;
        });
      }
    }
  }

  double _num(dynamic value) {
    if (value is num) return value.toDouble();
    return double.tryParse('$value') ?? 0.0;
  }

  double _pct(dynamic value) {
    final n = _num(value);
    return n.abs() <= 1.0 ? n * 100.0 : n;
  }

  int _int(dynamic value) {
    if (value is int) return value;
    if (value is num) return value.round();
    return int.tryParse('$value') ?? 0;
  }

  String _displayInt(
    bool loaded,
    dynamic value,
  ) {
    return loaded ? '${_int(value)}' : '—';
  }

  String _timeLabel() {
    final value = lastUpdated;
    if (value == null) {
      if (refreshing) return 'connecting…';
      return 'not connected';
    }
    final h = value.hour.toString().padLeft(2, '0');
    final m = value.minute.toString().padLeft(2, '0');
    final s = value.second.toString().padLeft(2, '0');
    return 'updated $h:$m:$s';
  }

  Map<String, dynamic> get _strategyMetrics {
    final raw = forwardStatus['strategy_metrics'];
    return raw is Map ? Map<String, dynamic>.from(raw) : {};
  }

  List<Map<String, dynamic>> get _compoundCandidates {
    final raw = compoundPayload['candidates'];
    return _mapList(raw);
  }

  List<Map<String, dynamic>> get _openCategoryPositions {
    return categoryPositions.where((row) {
      return '${row['status'] ?? ''}'.toUpperCase() == 'OPEN';
    }).toList();
  }

  Widget _card({
    required Widget child,
    EdgeInsets? padding,
  }) {
    return Container(
      width: double.infinity,
      padding: padding ?? const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .04),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(
          color: Colors.white.withValues(alpha: .075),
        ),
      ),
      child: child,
    );
  }

  Widget _pill(
    String text, {
    Color color = _blue,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(
        horizontal: 8,
        vertical: 5,
      ),
      decoration: BoxDecoration(
        color: color.withValues(alpha: .11),
        border: Border.all(
          color: color.withValues(alpha: .30),
        ),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: color,
          fontSize: 9,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }

  Widget _metric(
    String label,
    String value, {
    Color color = Colors.white,
  }) {
    return Expanded(
      child: Column(
        children: [
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: color,
              fontSize: 16,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            label,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: Colors.white38,
              fontSize: 8.5,
            ),
          ),
        ],
      ),
    );
  }

  Widget _sectionTitle(
    String title, {
    String? trailing,
  }) {
    return Row(
      children: [
        Expanded(
          child: Text(
            title,
            style: const TextStyle(
              color: _teal,
              fontSize: 15,
              fontWeight: FontWeight.w900,
            ),
          ),
        ),
        if (trailing != null)
          Text(
            trailing,
            style: const TextStyle(
              color: Colors.white38,
              fontSize: 9,
            ),
          ),
      ],
    );
  }

  Widget _header() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 10, 8),
      child: Row(
        children: [
          Container(
            width: 48,
            height: 48,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(14),
              gradient: const LinearGradient(
                colors: [
                  Color(0xFF65E6D3),
                  Color(0xFF6FA8FF),
                ],
                begin: Alignment.bottomLeft,
                end: Alignment.topRight,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.all(6),
              child: Image.asset(
                'assets/images/jasong_logo.png',
                fit: BoxFit.contain,
                errorBuilder: (_, __, ___) => const Icon(
                  Icons.auto_graph_rounded,
                  color: Colors.black,
                ),
              ),
            ),
          ),
          const SizedBox(width: 12),
          const Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Jasong AI Trader',
                  style: TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                SizedBox(height: 2),
                Text(
                  'V6.9.4-forward • Broker-Settled Intelligence • IG DEMO',
                  style: TextStyle(
                    fontSize: 9.2,
                    color: Colors.white54,
                  ),
                ),
              ],
            ),
          ),
          IconButton.filledTonal(
            onPressed: refreshing ? null : () => refreshAll(),
            icon: refreshing
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                    ),
                  )
                : const Icon(Icons.refresh_rounded),
          ),
        ],
      ),
    );
  }

  Widget _connectionBanner() {
    if (!refreshing && error == null) {
      return const SizedBox.shrink();
    }

    final message = error ??
        'Loading ${activeEndpoint ?? 'backend data'}…';

    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: _card(
        child: Row(
          children: [
            if (refreshing)
              const SizedBox(
                width: 14,
                height: 14,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                ),
              )
            else
              const Icon(
                Icons.warning_amber_rounded,
                size: 16,
                color: _amber,
              ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                message,
                style: TextStyle(
                  color: error == null
                      ? Colors.white54
                      : _amber,
                  fontSize: 9.5,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _homePage() {
    final forwardLoaded = forwardStatus.isNotEmpty;
    final portfolioLoaded = portfolioStatus.isNotEmpty;

    final metrics = _strategyMetrics.values
        .whereType<Map>()
        .map(
          (row) => Map<String, dynamic>.from(row),
        )
        .toList();

    final learningStrategies = metrics
        .where(
          (row) => _int(row['settled_trades']) > 0,
        )
        .length;

    final primeStrategies = metrics
        .where(
          (row) => row['prime_eligible'] == true,
        )
        .length;

    final liveMoney =
        forwardStatus['live_money_execution'] == true;

    final authority =
        '${forwardStatus['authority'] ?? 'BROKER_SETTLED_FORWARD_ONLY'}';

    final sourceMap = dataHealth['last_source_by_market'];
    final sourcesLoaded = sourceMap is Map && sourceMap.isNotEmpty;
    final sourceSummary = sourcesLoaded
        ? sourceMap.entries
            .take(3)
            .map((e) => '${e.key}: ${e.value}')
            .join(' • ')
        : 'market source telemetry loading';

    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(
          14,
          6,
          14,
          110,
        ),
        children: [
          _sectionTitle(
            'SYSTEM — FORWARD PRIME',
            trailing: _timeLabel(),
          ),
          const SizedBox(height: 10),
          _connectionBanner(),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                Wrap(
                  spacing: 7,
                  runSpacing: 7,
                  children: [
                    _pill(
                      'IG DEMO ONLY',
                      color: _green,
                    ),
                    _pill(
                      'FORWARD AUTHORITY',
                      color: _blue,
                    ),
                    _pill(
                      'HISTORY = INFO',
                      color: _purple,
                    ),
                    _pill(
                      liveMoney
                          ? 'LIVE MONEY ON'
                          : 'LIVE MONEY OFF',
                      color:
                          liveMoney ? _red : _green,
                    ),
                  ],
                ),
                const SizedBox(height: 13),
                Row(
                  children: [
                    _metric(
                      'SETTLED STORE',
                      _displayInt(
                        forwardLoaded,
                        forwardStatus[
                            'stored_settled_trades'],
                      ),
                    ),
                    _metric(
                      'OPEN JSCAT',
                      _displayInt(
                        portfolioLoaded,
                        portfolioStatus[
                            'open_positions'],
                      ),
                    ),
                    _metric(
                      'JSCAT CLOSED',
                      _displayInt(
                        portfolioLoaded,
                        portfolioStatus['closes'],
                      ),
                    ),
                    _metric(
                      'CAPACITY LEFT',
                      _displayInt(
                        portfolioLoaded,
                        portfolioStatus[
                            'global_remaining_positions'],
                      ),
                    ),
                  ],
                ),
                const Divider(
                  height: 24,
                  color: Colors.white10,
                ),
                Row(
                  children: [
                    _metric(
                      'LEARNING STRATS',
                      forwardLoaded
                          ? '$learningStrategies'
                          : '—',
                      color: _blue,
                    ),
                    _metric(
                      'PRIME STRATS',
                      forwardLoaded
                          ? '$primeStrategies'
                          : '—',
                      color: primeStrategies > 0
                          ? _amber
                          : Colors.white54,
                    ),
                    _metric(
                      'QUANT',
                      '≥28%',
                      color: _green,
                    ),
                    _metric(
                      'AI / FAST',
                      '40 / 45',
                      color: _green,
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 11),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                const Text(
                  'PRIME AUTHORITY',
                  style: TextStyle(
                    fontSize: 10,
                    fontWeight: FontWeight.w900,
                    color: _teal,
                  ),
                ),
                const SizedBox(height: 7),
                Text(
                  authority.replaceAll('_', ' '),
                  style: const TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 7),
                const Text(
                  'STRONG → controlled IG DEMO category learning → '
                  'broker settlement → strategy forward metrics → '
                  'PRIME → Compound.',
                  style: TextStyle(
                    color: Colors.white60,
                    fontSize: 10,
                    height: 1.4,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 11),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Expanded(
                      child: Text(
                        'MARKET DATA HEALTH',
                        style: TextStyle(
                          color: _teal,
                          fontSize: 10,
                          fontWeight:
                              FontWeight.w900,
                        ),
                      ),
                    ),
                    _pill(
                      dataHealth[
                                  'yahoo_cooldown_active'] ==
                              true
                          ? 'YAHOO COOLDOWN'
                          : 'DATA ROUTER READY',
                      color: dataHealth[
                                  'yahoo_cooldown_active'] ==
                              true
                          ? _amber
                          : _green,
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  sourceSummary,
                  style: const TextStyle(
                    color: Colors.white54,
                    fontSize: 9,
                    height: 1.35,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _compoundPage() {
    final rows = _compoundCandidates;
    final loaded = compoundPayload.isNotEmpty;
    final rule =
        '${compoundPayload['rule'] ?? ''}';

    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(
          14,
          6,
          14,
          110,
        ),
        children: [
          _sectionTitle(
            'COMPOUND — PRIME ONLY',
            trailing: _timeLabel(),
          ),
          const SizedBox(height: 10),
          _connectionBanner(),
          _card(
            child: Row(
              children: [
                _metric(
                  'CANDIDATES',
                  loaded ? '${rows.length}' : '—',
                  color: rows.isNotEmpty
                      ? _amber
                      : Colors.white54,
                ),
                _metric('RANK', '#1 / #2'),
                _metric('QUANT', '≥28%'),
                _metric('AI / FAST', '40 / 45'),
              ],
            ),
          ),
          if (rule.isNotEmpty) ...[
            const SizedBox(height: 9),
            Text(
              rule,
              style: const TextStyle(
                color: Colors.white54,
                fontSize: 9,
                height: 1.35,
              ),
            ),
          ],
          const SizedBox(height: 12),
          if (!loaded)
            _emptyMessage(
              'Compound candidates are still loading.',
            )
          else if (rows.isEmpty)
            _emptyMessage(
              'No PRIME Compound candidates yet. '
              'Strategies are still building broker-settled forward evidence.',
            )
          else
            ...rows.map(_compoundCandidateCard),
        ],
      ),
    );
  }

  Widget _compoundCandidateCard(
    Map<String, dynamic> row,
  ) {
    final market =
        '${row['market'] ?? row['symbol'] ?? '-'}';
    final direction =
        '${row['direction'] ?? '-'}'.toUpperCase();
    final strategy =
        '${row['strategy_name'] ?? row['strategy_id'] ?? '-'}';
    final quant = _pct(row['quant_confidence']);
    final ai = _pct(row['model_ai_confidence']);
    final fast = _num(
      row['live_fast_score'] ??
          row['smart_fast_score'],
    );

    final forwardRaw = row['forward_validation'];
    final forward = forwardRaw is Map
        ? Map<String, dynamic>.from(forwardRaw)
        : <String, dynamic>{};

    return Padding(
      padding: const EdgeInsets.only(bottom: 9),
      child: _card(
        child: Column(
          crossAxisAlignment:
              CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment:
                        CrossAxisAlignment.start,
                    children: [
                      Text(
                        market,
                        style: const TextStyle(
                          fontSize: 14,
                          fontWeight:
                              FontWeight.w900,
                        ),
                      ),
                      Text(
                        strategy,
                        style: const TextStyle(
                          color: Colors.white54,
                          fontSize: 9,
                        ),
                      ),
                    ],
                  ),
                ),
                _pill(
                  direction,
                  color: direction == 'BUY'
                      ? _green
                      : _red,
                ),
                const SizedBox(width: 6),
                _pill('PRIME', color: _amber),
              ],
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                _metric(
                  'QUANT',
                  '${quant.toStringAsFixed(1)}%',
                ),
                _metric(
                  'AI',
                  '${ai.toStringAsFixed(1)}%',
                ),
                _metric(
                  'FAST',
                  fast.toStringAsFixed(0),
                ),
                _metric(
                  'SETTLED',
                  '${_int(forward['settled_trades'])}',
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _tradesPage() {
    final openRows = _openCategoryPositions;
    final loaded =
        forwardTrades.isNotEmpty ||
        forwardStatus.isNotEmpty;

    final wins = forwardTrades.where((row) {
      return '${row['broker_result'] ?? ''}'
              .toUpperCase() ==
          'WIN';
    }).length;

    final losses = forwardTrades.where((row) {
      return '${row['broker_result'] ?? ''}'
              .toUpperCase() ==
          'LOSS';
    }).length;

    final total = forwardTrades.length;
    final wr = total > 0
        ? wins * 100.0 / total
        : 0.0;

    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(
          14,
          6,
          14,
          110,
        ),
        children: [
          _sectionTitle(
            'TRADES — IG DEMO',
            trailing: _timeLabel(),
          ),
          const SizedBox(height: 10),
          _connectionBanner(),
          _card(
            child: Row(
              children: [
                _metric(
                  'OPEN JSCAT',
                  categoryPositions.isEmpty &&
                          portfolioStatus.isEmpty
                      ? '—'
                      : '${openRows.length}',
                  color: _blue,
                ),
                _metric(
                  'SETTLED',
                  loaded ? '$total' : '—',
                ),
                _metric(
                  'WINS',
                  loaded ? '$wins' : '—',
                  color: _green,
                ),
                _metric(
                  'LOSSES',
                  loaded ? '$losses' : '—',
                  color: _red,
                ),
                _metric(
                  'WR',
                  loaded
                      ? '${wr.toStringAsFixed(1)}%'
                      : '—',
                ),
              ],
            ),
          ),
          const SizedBox(height: 13),
          const Text(
            'OPEN CATEGORY / LEARNING POSITIONS',
            style: TextStyle(
              color: Colors.white70,
              fontSize: 10,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 7),
          if (categoryPositions.isEmpty &&
              portfolioStatus.isEmpty)
            _emptyMessage('Positions loading…')
          else if (openRows.isEmpty)
            _emptyMessage('No open JSCAT positions')
          else
            ...openRows.take(20).map(_openTradeCard),
          const SizedBox(height: 13),
          const Text(
            'BROKER-SETTLED FORWARD EVIDENCE',
            style: TextStyle(
              color: Colors.white70,
              fontSize: 10,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 7),
          if (!loaded)
            _emptyMessage('Forward settlements loading…')
          else if (forwardTrades.isEmpty)
            _emptyMessage('No forward settlements loaded')
          else
            ...forwardTrades
                .take(50)
                .map(_settledTradeCard),
        ],
      ),
    );
  }

  Widget _openTradeCard(
    Map<String, dynamic> row,
  ) {
    final market =
        '${row['market'] ?? row['symbol'] ?? '-'}';
    final direction =
        '${row['direction'] ?? '-'}'.toUpperCase();
    final strategy =
        '${row['strategy_id'] ?? '-'}';

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: _card(
        child: Row(
          children: [
            Expanded(
              flex: 4,
              child: Column(
                crossAxisAlignment:
                    CrossAxisAlignment.start,
                children: [
                  Text(
                    market,
                    style: const TextStyle(
                      fontWeight:
                          FontWeight.w900,
                      fontSize: 12,
                    ),
                  ),
                  Text(
                    strategy,
                    maxLines: 1,
                    overflow:
                        TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white38,
                      fontSize: 8.5,
                    ),
                  ),
                ],
              ),
            ),
            _pill(
              direction,
              color: direction == 'BUY'
                  ? _green
                  : _red,
            ),
          ],
        ),
      ),
    );
  }

  Widget _settledTradeCard(
    Map<String, dynamic> row,
  ) {
    final market =
        '${row['market'] ?? row['symbol'] ?? '-'}';
    final result =
        '${row['broker_result'] ?? 'CLOSED'}'
            .toUpperCase();
    final strategy =
        '${row['strategy_id'] ?? 'UNKNOWN'}';
    final r = _num(row['r_multiple']);
    final color = result == 'WIN'
        ? _green
        : result == 'LOSS'
            ? _red
            : Colors.white54;

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: _card(
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment:
                    CrossAxisAlignment.start,
                children: [
                  Text(
                    market,
                    style: const TextStyle(
                      fontWeight:
                          FontWeight.w900,
                    ),
                  ),
                  Text(
                    strategy,
                    style: const TextStyle(
                      color: Colors.white38,
                      fontSize: 8.5,
                    ),
                  ),
                ],
              ),
            ),
            Text(
              result,
              style: TextStyle(
                color: color,
                fontWeight: FontWeight.w900,
              ),
            ),
            const SizedBox(width: 12),
            Text(
              '${r >= 0 ? '+' : ''}${r.toStringAsFixed(2)}R',
              style: TextStyle(
                color: color,
                fontWeight: FontWeight.w900,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _aiPage() {
    final entries = _strategyMetrics.entries.toList()
      ..sort((a, b) {
        final am = a.value is Map
            ? Map<String, dynamic>.from(
                a.value as Map,
              )
            : <String, dynamic>{};
        final bm = b.value is Map
            ? Map<String, dynamic>.from(
                b.value as Map,
              )
            : <String, dynamic>{};
        return _int(bm['settled_trades'])
            .compareTo(
          _int(am['settled_trades']),
        );
      });

    final findings = _mapList(
      forwardLearning['findings'],
    );

    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(
          14,
          6,
          14,
          110,
        ),
        children: [
          _sectionTitle(
            'AI — FORWARD LEARNING',
            trailing: _timeLabel(),
          ),
          const SizedBox(height: 10),
          _connectionBanner(),
          if (forwardStatus.isEmpty)
            _emptyMessage(
              'Forward strategy metrics loading…',
            )
          else if (entries.isEmpty)
            _emptyMessage(
              'No strategy forward metrics yet.',
            )
          else
            ...entries.map((entry) {
              final row = entry.value is Map
                  ? Map<String, dynamic>.from(
                      entry.value as Map,
                    )
                  : <String, dynamic>{};

              return Padding(
                padding:
                    const EdgeInsets.only(bottom: 8),
                child: _card(
                  child: Column(
                    crossAxisAlignment:
                        CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(
                              entry.key,
                              style: const TextStyle(
                                fontSize: 10,
                                fontWeight:
                                    FontWeight.w900,
                              ),
                            ),
                          ),
                          _pill(
                            '${row['state'] ?? 'BOOTSTRAP'}',
                            color:
                                row['prime_eligible'] ==
                                        true
                                    ? _amber
                                    : _blue,
                          ),
                        ],
                      ),
                      const SizedBox(height: 9),
                      Row(
                        children: [
                          _metric(
                            'SETTLED',
                            '${_int(row['settled_trades'])}',
                          ),
                          _metric(
                            'WR',
                            '${_pct(row['win_rate']).toStringAsFixed(1)}%',
                          ),
                          _metric(
                            'PF',
                            _num(row['profit_factor'])
                                .toStringAsFixed(2),
                          ),
                          _metric(
                            'EXP R',
                            _num(row['expectancy_r'])
                                .toStringAsFixed(2),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              );
            }),
          const SizedBox(height: 10),
          const Text(
            'RECURRING LEARNING FINDINGS',
            style: TextStyle(
              color: Colors.white70,
              fontSize: 10,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 7),
          if (forwardLearning.isEmpty)
            _emptyMessage('Learning report loading…')
          else if (findings.isEmpty)
            _emptyMessage(
              'No repeated strategy mistake has crossed the minimum occurrence threshold.',
            )
          else
            ...findings.map((row) {
              final name =
                  '${row['finding'] ?? row['name'] ?? 'Finding'}';
              final count =
                  _int(row['occurrences'] ?? row['count']);
              final recommendation =
                  '${row['recommendation'] ?? row['message'] ?? ''}';

              return Padding(
                padding:
                    const EdgeInsets.only(bottom: 8),
                child: _card(
                  child: Column(
                    crossAxisAlignment:
                        CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(
                              name,
                              style: const TextStyle(
                                fontWeight:
                                    FontWeight.w900,
                              ),
                            ),
                          ),
                          _pill(
                            '$count OCCURRENCES',
                            color: _amber,
                          ),
                        ],
                      ),
                      if (recommendation.isNotEmpty) ...[
                        const SizedBox(height: 7),
                        Text(
                          recommendation,
                          style: const TextStyle(
                            color: Colors.white54,
                            fontSize: 9.5,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              );
            }),
        ],
      ),
    );
  }

  Widget _settingsPage() {
    final thresholdsRaw =
        forwardStatus['thresholds'];
    final thresholds = thresholdsRaw is Map
        ? Map<String, dynamic>.from(
            thresholdsRaw,
          )
        : <String, dynamic>{};

    final historyRaw =
        forwardStatus['historical_validation'];
    final history = historyRaw is Map
        ? Map<String, dynamic>.from(historyRaw)
        : <String, dynamic>{};

    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(
          14,
          6,
          14,
          110,
        ),
        children: [
          _sectionTitle(
            'SETTINGS — RUNTIME VIEW',
            trailing: 'read-only mobile',
          ),
          const SizedBox(height: 10),
          _connectionBanner(),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                const Text(
                  'BACKEND',
                  style: TextStyle(
                    color: _teal,
                    fontSize: 9,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 6),
                SelectableText(
                  apiBase,
                  style: const TextStyle(
                    color: Colors.white70,
                    fontSize: 10,
                  ),
                ),
                const SizedBox(height: 8),
                _settingRow(
                  'Connection',
                  lastUpdated == null
                      ? 'WAITING'
                      : 'CONNECTED',
                ),
                _settingRow(
                  'Last update',
                  _timeLabel(),
                ),
                _settingRow(
                  'Data cache',
                  '${dataHealth['memory_entries'] ?? '—'} markets',
                ),
              ],
            ),
          ),
          const SizedBox(height: 10),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                const Text(
                  'FORWARD PRIME THRESHOLDS',
                  style: TextStyle(
                    color: _teal,
                    fontSize: 9,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 8),
                _settingRow(
                  'Minimum settled trades',
                  '${thresholds['min_settled_trades_for_prime'] ?? 12}',
                ),
                _settingRow(
                  'Profit factor',
                  '≥ ${_num(thresholds['min_profit_factor'] ?? 1.2).toStringAsFixed(2)}',
                ),
                _settingRow(
                  'Expectancy',
                  '≥ +${_num(thresholds['min_expectancy_r'] ?? .05).toStringAsFixed(2)}R',
                ),
                _settingRow(
                  'Win rate',
                  '≥ ${_pct(thresholds['min_win_rate'] ?? .45).toStringAsFixed(0)}%',
                ),
                _settingRow(
                  'Bootstrap',
                  '≥ ${_pct(thresholds['min_bootstrap_prob_positive_expectancy'] ?? .75).toStringAsFixed(0)}%',
                ),
                _settingRow(
                  'Max drawdown',
                  '≤ ${_num(thresholds['max_drawdown_r'] ?? 6).toStringAsFixed(1)}R',
                ),
              ],
            ),
          ),
          const SizedBox(height: 10),
          _card(
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                const Text(
                  'HISTORICAL VALIDATION',
                  style: TextStyle(
                    color: _teal,
                    fontSize: 9,
                    fontWeight: FontWeight.w900,
                  ),
                ),
                const SizedBox(height: 8),
                _settingRow(
                  'Mode',
                  '${history['mode'] ?? 'INFORMATIONAL_ONLY'}',
                ),
                _settingRow(
                  'Execution veto',
                  '${history['execution_veto'] ?? false}',
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _settingRow(
    String label,
    String value,
  ) {
    return Padding(
      padding:
          const EdgeInsets.symmetric(vertical: 5),
      child: Row(
        children: [
          Expanded(
            child: Text(
              label,
              style: const TextStyle(
                color: Colors.white54,
                fontSize: 9.5,
              ),
            ),
          ),
          const SizedBox(width: 10),
          Flexible(
            child: Text(
              value,
              textAlign: TextAlign.right,
              style: const TextStyle(
                fontSize: 9.5,
                fontWeight: FontWeight.w900,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _emptyMessage(String text) {
    return _card(
      child: Padding(
        padding: const EdgeInsets.symmetric(
          vertical: 20,
        ),
        child: Text(
          text,
          textAlign: TextAlign.center,
          style: const TextStyle(
            color: Colors.white38,
            fontSize: 10,
            height: 1.35,
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final pages = <Widget>[
      _homePage(),
      MarketCategoriesPage(
        apiBase: apiBase,
      ),
      _compoundPage(),
      _tradesPage(),
      _aiPage(),
      _settingsPage(),
    ];

    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            _header(),
            Expanded(
              child: IndexedStack(
                index: selectedTab,
                children: pages,
              ),
            ),
          ],
        ),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: selectedTab,
        onDestinationSelected: (index) {
          setState(() {
            selectedTab = index;
          });

          if (index != 1 && !refreshing) {
            refreshAll(silent: true);
          }
        },
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.home_outlined),
            selectedIcon: Icon(Icons.home_rounded),
            label: 'Home',
          ),
          NavigationDestination(
            icon: Icon(Icons.radar_outlined),
            selectedIcon: Icon(Icons.radar_rounded),
            label: 'Markets',
          ),
          NavigationDestination(
            icon: Icon(Icons.grid_view_outlined),
            selectedIcon: Icon(Icons.grid_view_rounded),
            label: 'Compound',
          ),
          NavigationDestination(
            icon: Icon(Icons.receipt_long_outlined),
            selectedIcon: Icon(Icons.receipt_long_rounded),
            label: 'Trades',
          ),
          NavigationDestination(
            icon: Icon(Icons.psychology_outlined),
            selectedIcon: Icon(Icons.psychology_rounded),
            label: 'AI',
          ),
          NavigationDestination(
            icon: Icon(Icons.settings_outlined),
            selectedIcon: Icon(Icons.settings_rounded),
            label: 'Settings',
          ),
        ],
      ),
    );
  }
}
