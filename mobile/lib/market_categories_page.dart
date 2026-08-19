import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

class MarketCategoriesPage extends StatefulWidget {
  final String apiBase;

  const MarketCategoriesPage({
    super.key,
    required this.apiBase,
  });

  @override
  State<MarketCategoriesPage> createState() => _MarketCategoriesPageState();
}

class _MarketCategoriesPageState extends State<MarketCategoriesPage> {
  static const _teal = Color(0xFF65E6D3);
  static const _green = Color(0xFF67F0C1);
  static const _red = Color(0xFFFF7E8B);
  static const _amber = Color(0xFFFFD75E);
  static const _blue = Color(0xFF6FA8FF);
  static const _purple = Color(0xFFB899FF);

  static const categories = <String>[
    'FOREX',
    'INDICES',
    'CRYPTO',
    'METALS',
    'ENERGY',
    'SHARES',
  ];

  String selected = 'FOREX';
  Map<String, dynamic>? systemStatus;
  Map<String, dynamic>? portfolioStatus;
  Map<String, dynamic>? forwardStatus;
  List<Map<String, dynamic>> selections = const [];

  bool busy = false;
  bool _refreshInFlight = false;
  String? error;
  Timer? livePollTimer;
  DateTime? lastGoodRefreshAt;
  final http.Client _client = http.Client();

  @override
  void initState() {
    super.initState();
    refreshAll();
    // The backend already refreshes its intelligence continuously.
    // Poll at 30s to avoid mobile request bursts against Render.
    livePollTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => refreshAll(silent: true),
    );
  }

  @override
  void dispose() {
    livePollTimer?.cancel();
    _client.close();
    super.dispose();
  }

  bool _isTransientBackendError(Object value) {
    final text = value.toString().toLowerCase();
    return value is TimeoutException ||
        value is SocketException ||
        value is http.ClientException ||
        text.contains('http 502') ||
        text.contains('http 503') ||
        text.contains('http 504') ||
        text.contains('connection reset') ||
        text.contains('connection closed') ||
        text.contains('timed out') ||
        text.contains('timeout');
  }

  String _friendlyError(Object value) {
    final text = value.toString().toLowerCase();
    if (text.contains('502')) {
      return 'Market server is temporarily busy (HTTP 502). Last good market data is being kept. Automatic retry is active.';
    }
    if (text.contains('503')) {
      return 'Market server is temporarily unavailable (HTTP 503). Last good market data is being kept. Automatic retry is active.';
    }
    if (text.contains('504') || value is TimeoutException) {
      return 'Market request timed out. Last good market data is being kept. Automatic retry is active.';
    }
    if (value is SocketException || value is http.ClientException) {
      return 'Network connection interrupted. Last good market data is being kept. Automatic retry is active.';
    }
    if (value is FormatException) {
      return 'The server returned an invalid response. Last good market data is still displayed.';
    }
    return 'Could not refresh market intelligence. Last good market data is still displayed.';
  }

  Future<Map<String, dynamic>> _get(
    String path, {
    int timeoutSeconds = 40,
  }) async {
    final response = await _client
        .get(
          Uri.parse('${widget.apiBase}$path'),
          headers: const {'Accept': 'application/json'},
        )
        .timeout(Duration(seconds: timeoutSeconds));

    if (response.statusCode != 200) {
      // Do not put Render's full HTML error page into the Flutter widget tree.
      throw HttpException('HTTP ${response.statusCode}');
    }

    final contentType = response.headers['content-type'] ?? '';
    if (!contentType.toLowerCase().contains('json') &&
        response.body.trimLeft().startsWith('<')) {
      throw const FormatException('Backend returned HTML instead of JSON');
    }

    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Unexpected backend response');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>> _getWithRetry(
    String path, {
    int attempts = 2,
  }) async {
    Object? lastError;
    for (var attempt = 1; attempt <= attempts; attempt++) {
      try {
        return await _get(path);
      } catch (e) {
        lastError = e;
        if (!_isTransientBackendError(e) || attempt >= attempts) {
          rethrow;
        }
        await Future.delayed(Duration(seconds: attempt * 2));
      }
    }
    throw lastError ?? const HttpException('Market request failed');
  }

  Future<Map<String, dynamic>?> _optionalGet(String path) async {
    try {
      return await _getWithRetry(path);
    } catch (_) {
      return null;
    }
  }

  Future<Map<String, dynamic>> _post(String path) async {
    final response = await _client
        .post(
          Uri.parse('${widget.apiBase}$path'),
          headers: const {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
          },
          body: '{}',
        )
        .timeout(const Duration(seconds: 180));

    if (response.statusCode != 200) {
      // A POST may already have reached the backend, so do not auto-repeat it.
      throw HttpException('HTTP ${response.statusCode}');
    }

    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Unexpected backend response');
    }
    return Map<String, dynamic>.from(decoded);
  }

  List<Map<String, dynamic>> _mapList(dynamic value) {
    if (value is! List) return const [];
    return value
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  double _num(dynamic value) {
    if (value is num) return value.toDouble();
    return double.tryParse('$value') ?? 0.0;
  }

  double _pct(dynamic value) {
    final n = _num(value);
    return n.abs() <= 1.0 ? n * 100.0 : n;
  }

  Future<void> refreshAll({bool silent = false}) async {
    if (_refreshInFlight) return;
    _refreshInFlight = true;

    if (!silent && mounted) {
      setState(() {
        busy = true;
        error = null;
      });
    }

    Object? criticalError;
    try {
      // Fetch the selected ranking first because this is the data the user is
      // actually looking at. The remaining status calls are deliberately
      // sequential and optional, avoiding the four-request burst that was
      // causing Render 502 responses in the previous mobile build.
      Map<String, dynamic>? categoryPayload;
      try {
        categoryPayload = await _getWithRetry(
          '/market-categories/$selected',
          attempts: 2,
        );
      } catch (e) {
        criticalError = e;
      }

      final statusPayload = await _optionalGet('/market-categories/status');
      final portfolioPayload = await _optionalGet('/category-portfolio/status');
      final forwardPayload = await _optionalGet('/forward-validation/status');

      if (!mounted) return;
      setState(() {
        if (categoryPayload != null) {
          final next = _mapList(categoryPayload['selections']);
          selections = next;
          lastGoodRefreshAt = DateTime.now();
        }
        if (statusPayload != null) systemStatus = statusPayload;
        if (portfolioPayload != null) portfolioStatus = portfolioPayload;
        if (forwardPayload != null) forwardStatus = forwardPayload;

        if (criticalError != null) {
          // Silent polls still surface one compact warning, but never raw HTML.
          error = _friendlyError(criticalError!);
        } else {
          error = null;
        }
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => error = _friendlyError(e));
    } finally {
      _refreshInFlight = false;
      if (!silent && mounted) {
        setState(() => busy = false);
      }
    }
  }

  Future<void> selectCategory(String category) async {
    if (category == selected) return;
    setState(() {
      selected = category;
      selections = const [];
    });
    await refreshAll();
  }

  Future<void> _run(String path) async {
    if (busy) return;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await _post(path);
      await refreshAll(silent: true);
    } catch (e) {
      if (mounted) setState(() => error = _friendlyError(e));
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Widget _pill(String text, {Color color = _blue}) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
      decoration: BoxDecoration(
        color: color.withValues(alpha: .11),
        border: Border.all(color: color.withValues(alpha: .32)),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: color,
          fontSize: 9.5,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }

  Color _directionColor(String direction) {
    if (direction == 'BUY') return _green;
    if (direction == 'SELL') return _red;
    return _amber;
  }

  Map<String, dynamic> _forward(Map<String, dynamic> item) {
    final raw = item['forward_validation'];
    return raw is Map
        ? Map<String, dynamic>.from(raw)
        : <String, dynamic>{};
  }

  Map<String, dynamic> _provenance(Map<String, dynamic> item) {
    final raw = item['provenance'];
    return raw is Map
        ? Map<String, dynamic>.from(raw)
        : <String, dynamic>{};
  }

  Widget _summaryCard() {
    final strongCount = selections.where((item) {
      return item['strong_qualified'] == true ||
          '${item['trade_class'] ?? ''}'.toUpperCase() == 'STRONG';
    }).length;

    final primeCount = selections.where((item) {
      final forward = _forward(item);
      return item['prime_qualified'] == true ||
          item['compound_eligible'] == true ||
          forward['prime_eligible'] == true;
    }).length;

    final openByCategory = portfolioStatus?['open_by_category'];
    final open = openByCategory is Map ? (openByCategory[selected] ?? 0) : 0;

    final firstForward = selections.isNotEmpty
        ? _forward(selections.first)
        : <String, dynamic>{};
    final settled = firstForward['settled_trades'] ?? 0;
    final forwardState = '${firstForward['state'] ?? 'BOOTSTRAP'}';

    final thresholdsRaw = forwardStatus?['thresholds'];
    final thresholds = thresholdsRaw is Map
        ? Map<String, dynamic>.from(thresholdsRaw)
        : <String, dynamic>{};

    final minTrades = thresholds['min_settled_trades_for_prime'] ?? 12;
    final minPf = _num(thresholds['min_profit_factor'] ?? 1.2);
    final minExp = _num(thresholds['min_expectancy_r'] ?? 0.05);
    final minWr = _pct(thresholds['min_win_rate'] ?? .45);
    final minBootstrap = _pct(
      thresholds['min_bootstrap_prob_positive_expectancy'] ?? .75,
    );
    final maxDd = _num(thresholds['max_drawdown_r'] ?? 6.0);

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(15),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .045),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: Colors.white.withValues(alpha: .08)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '$selected FORWARD INTELLIGENCE',
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    const SizedBox(height: 3),
                    const Text(
                      'Broker-settled evidence is PRIME authority',
                      style: TextStyle(color: Colors.white54, fontSize: 10),
                    ),
                  ],
                ),
              ),
              _pill('28 / 40 / 45', color: _teal),
            ],
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 7,
            runSpacing: 7,
            children: [
              _pill('$strongCount STRONG', color: _green),
              _pill('$primeCount PRIME', color: primeCount > 0 ? _amber : Colors.white54),
              _pill('$open OPEN', color: _purple),
              _pill('$settled / $minTrades FORWARD', color: _blue),
              _pill(forwardState, color: forwardState == 'PRIME' ? _amber : _blue),
            ],
          ),
          const SizedBox(height: 11),
          Text(
            'PRIME: ≥$minTrades settled • PF ≥${minPf.toStringAsFixed(2)} • '
            'Exp ≥+${minExp.toStringAsFixed(2)}R • WR ≥${minWr.toStringAsFixed(0)}% • '
            'Bootstrap ≥${minBootstrap.toStringAsFixed(0)}% • DD ≤${maxDd.toStringAsFixed(1)}R',
            style: const TextStyle(
              color: Colors.white60,
              fontSize: 9.5,
              height: 1.35,
            ),
          ),
          const SizedBox(height: 4),
          const Text(
            'Historical holdout / walk-forward evidence is informational only and cannot veto execution.',
            style: TextStyle(color: Colors.white38, fontSize: 8.8, height: 1.3),
          ),
        ],
      ),
    );
  }

  Widget _metric(String label, String value, {Color color = Colors.white}) {
    return Expanded(
      child: Column(
        children: [
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: color,
              fontSize: 14,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            label,
            textAlign: TextAlign.center,
            style: const TextStyle(color: Colors.white38, fontSize: 8.5),
          ),
        ],
      ),
    );
  }

  Widget _selectionCard(Map<String, dynamic> item) {
    final rank = item['category_rank'] ?? item['rank'] ?? '-';
    final symbol = '${item['market'] ?? item['name'] ?? item['symbol'] ?? '-'}';
    final direction = '${item['direction'] ?? 'WAIT'}'.toUpperCase();
    final strategy = '${item['strategy_name'] ?? item['strategy_id'] ?? '-'}';
    final regime = '${item['market_regime'] ?? item['regime'] ?? '-'}'
        .replaceAll('_', ' ');

    final quant = item['quant_confidence_pct'] != null
        ? _num(item['quant_confidence_pct'])
        : _pct(item['quant_confidence']);
    final ai = item['model_ai_directional_confidence_pct'] != null
        ? _num(item['model_ai_directional_confidence_pct'])
        : _pct(item['model_ai_confidence']);
    final fast = _num(item['live_fast_score'] ?? item['smart_fast_score']);
    final spread = _num(item['ig_spread_bps'] ?? item['spread_bps']);

    final strong = item['strong_qualified'] == true ||
        '${item['trade_class'] ?? ''}'.toUpperCase() == 'STRONG';
    final learning = item['ig_demo_learning_eligible'] == true ||
        item['learning_eligible'] == true;

    final forward = _forward(item);
    final prime = item['prime_qualified'] == true ||
        item['compound_eligible'] == true ||
        forward['prime_eligible'] == true;
    final settled = forward['settled_trades'] ?? 0;
    final wins = forward['wins'] ?? 0;
    final losses = forward['losses'] ?? 0;
    final wr = _num(forward['win_rate_pct']);
    final pf = _num(forward['profit_factor']);
    final expR = _num(forward['expectancy_r']);
    final ddR = _num(forward['max_drawdown_r']);
    final bootstrap = _num(
      forward['bootstrap_probability_positive_expectancy_pct'],
    );
    final forwardState = '${forward['state'] ?? 'BOOTSTRAP'}';

    final provenance = _provenance(item);
    final analysisSource = '${item['analysis_price_source'] ?? provenance['analysis_price_source'] ?? 'UNKNOWN'}';
    final quoteSource = '${item['broker_quote_source'] ?? provenance['broker_quote_source'] ?? 'UNKNOWN'}';
    final fresh = provenance['fresh'] == true;

    final rejectionRaw = item['rejection_reasons'];
    final rejectionReasons = rejectionRaw is List
        ? rejectionRaw.map((e) => '$e').toList()
        : const <String>[];

    final stateColor = prime
        ? _amber
        : strong
            ? _green
            : Colors.white54;
    final stateLabel = prime
        ? 'PRIME'
        : strong
            ? 'STRONG'
            : 'WATCH';

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .04),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(
          color: stateColor.withValues(alpha: prime ? .42 : .18),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 32,
                height: 32,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: .06),
                  shape: BoxShape.circle,
                ),
                child: Text(
                  '#$rank',
                  style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 11),
                ),
              ),
              const SizedBox(width: 9),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      symbol,
                      style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 14.5),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      '$regime • $strategy',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: Colors.white54, fontSize: 9.5),
                    ),
                  ],
                ),
              ),
              _pill(direction, color: _directionColor(direction)),
              const SizedBox(width: 5),
              _pill(stateLabel, color: stateColor),
            ],
          ),
          const SizedBox(height: 11),
          Row(
            children: [
              _metric('QUANT', '${quant.toStringAsFixed(1)}%', color: quant >= 28 ? _green : _red),
              _metric('AI', '${ai.toStringAsFixed(1)}%', color: ai >= 40 ? _green : _red),
              _metric('FAST', fast.toStringAsFixed(0), color: fast >= 45 ? _green : _red),
              _metric('SPREAD', '${spread.toStringAsFixed(2)} bps'),
            ],
          ),
          const Divider(height: 20, color: Colors.white10),
          Row(
            children: [
              _metric('SETTLED', '$settled / 12', color: settled >= 12 ? _green : _blue),
              _metric('W/L', '$wins / $losses'),
              _metric('WR', '${wr.toStringAsFixed(1)}%'),
              _metric('PF', pf.toStringAsFixed(2)),
            ],
          ),
          const SizedBox(height: 9),
          Row(
            children: [
              _metric('EXPECTANCY', '${expR >= 0 ? '+' : ''}${expR.toStringAsFixed(2)}R'),
              _metric('DRAWDOWN', '${ddR.toStringAsFixed(2)}R'),
              _metric('BOOTSTRAP', '${bootstrap.toStringAsFixed(1)}%'),
              _metric('FORWARD', forwardState, color: prime ? _amber : _blue),
            ],
          ),
          const SizedBox(height: 11),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _pill('ANALYSIS $analysisSource', color: _blue),
              _pill('QUOTE $quoteSource', color: _purple),
              _pill(fresh ? 'FRESH' : 'STALE', color: fresh ? _green : _red),
              if (learning) _pill('IG DEMO LEARNING', color: _green),
              if (prime) _pill('COMPOUND ELIGIBLE', color: _amber),
            ],
          ),
          if (rejectionReasons.isNotEmpty) ...[
            const SizedBox(height: 9),
            Text(
              rejectionReasons.join(' • '),
              style: const TextStyle(color: Colors.white38, fontSize: 8.8, height: 1.3),
            ),
          ],
        ],
      ),
    );
  }

  Widget _actionButton({
    required String label,
    required IconData icon,
    required VoidCallback? onPressed,
    bool primary = false,
  }) {
    return Expanded(
      child: SizedBox(
        height: 42,
        child: primary
            ? FilledButton.icon(
                onPressed: onPressed,
                icon: Icon(icon, size: 17),
                label: Text(label, style: const TextStyle(fontSize: 10, fontWeight: FontWeight.w800)),
              )
            : OutlinedButton.icon(
                onPressed: onPressed,
                icon: Icon(icon, size: 17),
                label: Text(label, style: const TextStyle(fontSize: 10, fontWeight: FontWeight.w800)),
              ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(14, 6, 14, 110),
        children: [
          Row(
            children: [
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'FORWARD MARKET INTELLIGENCE',
                      style: TextStyle(color: _teal, fontSize: 16, fontWeight: FontWeight.w900),
                    ),
                    SizedBox(height: 3),
                    Text(
                      'V6.9.4-forward • broker-settled categories • IG DEMO',
                      style: TextStyle(color: Colors.white54, fontSize: 9.5),
                    ),
                  ],
                ),
              ),
              IconButton(
                onPressed: busy ? null : () => refreshAll(),
                icon: busy
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.refresh_rounded),
              ),
            ],
          ),
          const SizedBox(height: 11),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: categories.map((category) {
                final active = selected == category;
                return Padding(
                  padding: const EdgeInsets.only(right: 7),
                  child: ChoiceChip(
                    selected: active,
                    label: Text(category),
                    onSelected: (_) => selectCategory(category),
                    labelStyle: TextStyle(
                      fontSize: 9.5,
                      fontWeight: FontWeight.w800,
                      color: active ? Colors.black : Colors.white70,
                    ),
                    selectedColor: _teal,
                    backgroundColor: Colors.white.withValues(alpha: .04),
                    side: BorderSide(color: Colors.white.withValues(alpha: .09)),
                  ),
                );
              }).toList(),
            ),
          ),
          const SizedBox(height: 11),
          _summaryCard(),
          const SizedBox(height: 10),
          Row(
            children: [
              _actionButton(
                label: 'SCAN $selected',
                icon: Icons.radar_rounded,
                onPressed: busy ? null : () => _run('/market-categories/$selected/run-now'),
                primary: true,
              ),
              const SizedBox(width: 7),
              _actionButton(
                label: 'SCAN ALL',
                icon: Icons.public_rounded,
                onPressed: busy ? null : () => _run('/market-categories/run-now'),
              ),
              const SizedBox(width: 7),
              _actionButton(
                label: 'PORTFOLIO',
                icon: Icons.play_arrow_rounded,
                onPressed: busy ? null : () => _run('/category-portfolio/run-now'),
              ),
            ],
          ),
          if (error != null) ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: _red.withValues(alpha: .10),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: _red.withValues(alpha: .25)),
              ),
              child: Text(
                error!,
                style: const TextStyle(color: _red, fontSize: 9.5),
              ),
            ),
          ],
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: Text(
                  '$selected RANKINGS',
                  style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w900, color: Colors.white70),
                ),
              ),
              Text(
                '${selections.length} markets',
                style: const TextStyle(color: Colors.white38, fontSize: 9),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (selections.isEmpty)
            Container(
              padding: const EdgeInsets.symmetric(vertical: 36, horizontal: 18),
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: .025),
                borderRadius: BorderRadius.circular(18),
                border: Border.all(color: Colors.white.withValues(alpha: .06)),
              ),
              child: const Text(
                'No current category selections. Pull to refresh or run a category scan.',
                textAlign: TextAlign.center,
                style: TextStyle(color: Colors.white38, fontSize: 10),
              ),
            )
          else
            ...selections.map(_selectionCard),
        ],
      ),
    );
  }
}
