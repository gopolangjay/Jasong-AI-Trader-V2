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
  State<MarketCategoriesPage> createState() =>
      _MarketCategoriesPageState();
}

class _MarketCategoriesPageState
    extends State<MarketCategoriesPage> {
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

  final http.Client _client = http.Client();

  String selected = 'FOREX';
  List<Map<String, dynamic>> selections = [];
  Map<String, dynamic> portfolioStatus = {};
  Map<String, dynamic> forwardStatus = {};
  Map<String, dynamic> dataHealth = {};

  bool busy = false;
  bool refreshInFlight = false;
  String? error;
  String? loadingStep;
  DateTime? lastUpdated;
  Timer? pollTimer;

  @override
  void initState() {
    super.initState();
    Future.microtask(() => refreshAll());

    pollTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) {
        if (!refreshInFlight) {
          refreshAll(silent: true);
        }
      },
    );
  }

  @override
  void dispose() {
    pollTimer?.cancel();
    _client.close();
    super.dispose();
  }

  Future<Map<String, dynamic>> _get(
    String path, {
    int timeoutSeconds = 20,
  }) async {
    final response = await _client
        .get(
          Uri.parse('${widget.apiBase}$path'),
          headers: const {
            'Accept': 'application/json',
            'Cache-Control': 'no-cache',
          },
        )
        .timeout(
          Duration(seconds: timeoutSeconds),
        );

    if (response.statusCode != 200) {
      throw HttpException(
        'HTTP ${response.statusCode}',
      );
    }

    final body = response.body.trim();
    if (body.startsWith('<')) {
      throw const FormatException(
        'Backend returned HTML',
      );
    }

    final decoded = jsonDecode(body);
    if (decoded is! Map) {
      throw const FormatException(
        'Expected JSON object',
      );
    }

    return Map<String, dynamic>.from(
      decoded,
    );
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

  String _friendlyError(
    String path,
    Object e,
  ) {
    final text = e.toString().toLowerCase();

    if (text.contains('502')) {
      return '$path: Render is temporarily busy (HTTP 502).';
    }
    if (text.contains('503')) {
      return '$path: backend unavailable (HTTP 503).';
    }
    if (text.contains('504') ||
        e is TimeoutException) {
      return '$path: request timed out.';
    }
    if (e is SocketException ||
        e is http.ClientException) {
      return '$path: network connection interrupted.';
    }
    return '$path: could not load market data.';
  }

  Future<Map<String, dynamic>> _getRetry(
    String path, {
    int attempts = 2,
    int timeoutSeconds = 20,
  }) async {
    Object? lastError;

    for (var attempt = 1;
        attempt <= attempts;
        attempt++) {
      try {
        return await _get(
          path,
          timeoutSeconds: timeoutSeconds,
        );
      } catch (e) {
        lastError = e;

        if (!_transient(e) ||
            attempt >= attempts) {
          rethrow;
        }

        await Future.delayed(
          Duration(seconds: attempt * 2),
        );
      }
    }

    throw lastError ??
        const HttpException(
          'Unknown market request failure',
        );
  }

  List<Map<String, dynamic>> _mapList(
    dynamic value,
  ) {
    if (value is! List) return const [];
    return value
        .whereType<Map>()
        .map(
          (row) =>
              Map<String, dynamic>.from(row),
        )
        .toList();
  }

  Future<void> _loadStep(
    String path,
    void Function(Map<String, dynamic>) apply, {
    int timeoutSeconds = 20,
    bool silent = false,
  }) async {
    if (!mounted) return;

    setState(() {
      loadingStep = path;
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

  Future<void> refreshAll({
    bool silent = false,
  }) async {
    if (refreshInFlight) return;

    refreshInFlight = true;

    if (mounted) {
      setState(() {
        busy = true;
        if (!silent) error = null;
      });
    }

    try {
      // Critical UX change:
      // selected market ranking is applied immediately.
      await _loadStep(
        '/market-categories/$selected',
        (payload) {
          selections = _mapList(
            payload['selections'],
          );
        },
        timeoutSeconds: 25,
        silent: silent,
      );

      // These status calls fill summary badges later.
      // They do NOT block the selected market rows.
      await _loadStep(
        '/category-portfolio/status',
        (payload) =>
            portfolioStatus = payload,
        timeoutSeconds: 15,
        silent: true,
      );

      await _loadStep(
        '/forward-validation/status',
        (payload) =>
            forwardStatus = payload,
        timeoutSeconds: 20,
        silent: true,
      );

      await _loadStep(
        '/market-categories/data-health',
        (payload) =>
            dataHealth = payload,
        timeoutSeconds: 15,
        silent: true,
      );
    } finally {
      refreshInFlight = false;
      if (mounted) {
        setState(() {
          busy = false;
          loadingStep = null;
        });
      }
    }
  }

  Future<void> selectCategory(
    String category,
  ) async {
    if (category == selected) return;

    setState(() {
      selected = category;
      selections = [];
      error = null;
    });

    await refreshAll();
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

  Map<String, dynamic> _forward(
    Map<String, dynamic> row,
  ) {
    final raw = row['forward_validation'];
    return raw is Map
        ? Map<String, dynamic>.from(raw)
        : <String, dynamic>{};
  }

  Map<String, dynamic> _provenance(
    Map<String, dynamic> row,
  ) {
    final raw = row['provenance'];
    return raw is Map
        ? Map<String, dynamic>.from(raw)
        : <String, dynamic>{};
  }

  Color _directionColor(String direction) {
    if (direction == 'BUY') return _green;
    if (direction == 'SELL') return _red;
    return _amber;
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
              fontSize: 14,
              fontWeight: FontWeight.w900,
            ),
          ),
          const SizedBox(height: 2),
          Text(
            label,
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: Colors.white38,
              fontSize: 8,
            ),
          ),
        ],
      ),
    );
  }

  Widget _summaryCard() {
    final strong = selections.where((row) {
      return row['strong_qualified'] == true ||
          '${row['trade_class'] ?? ''}'
                  .toUpperCase() ==
              'STRONG';
    }).length;

    final prime = selections.where((row) {
      return row['prime_qualified'] == true ||
          _forward(row)['prime_eligible'] == true;
    }).length;

    final openByCategory =
        portfolioStatus['open_by_category'];
    final open = openByCategory is Map
        ? _int(openByCategory[selected])
        : 0;

    final strategyMetrics =
        forwardStatus['strategy_metrics'];
    var categorySettled = 0;

    if (strategyMetrics is Map &&
        selections.isNotEmpty) {
      final strategies = selections
          .map(
            (row) =>
                '${row['strategy_id'] ?? ''}',
          )
          .where((id) => id.isNotEmpty)
          .toSet();

      for (final id in strategies) {
        final metric = strategyMetrics[id];
        if (metric is Map) {
          categorySettled +=
              _int(metric['settled_trades']);
        }
      }
    }

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .04),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(
          color: Colors.white.withValues(alpha: .075),
        ),
      ),
      child: Column(
        crossAxisAlignment:
            CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '$selected — FORWARD INTELLIGENCE',
                  style: const TextStyle(
                    color: _teal,
                    fontSize: 15,
                    fontWeight: FontWeight.w900,
                  ),
                ),
              ),
              _pill(
                '28 / 40 / 45',
                color: _teal,
              ),
            ],
          ),
          const SizedBox(height: 11),
          Row(
            children: [
              _metric(
                'ROWS',
                selections.isEmpty && busy
                    ? '…'
                    : '${selections.length}',
              ),
              _metric(
                'STRONG',
                '$strong',
                color: _green,
              ),
              _metric(
                'PRIME',
                '$prime',
                color: prime > 0
                    ? _amber
                    : Colors.white54,
              ),
              _metric(
                'OPEN',
                '$open',
                color: _purple,
              ),
              _metric(
                'SETTLED',
                '$categorySettled',
                color: _blue,
              ),
            ],
          ),
          const SizedBox(height: 10),
          const Text(
            'Historical holdout and walk-forward values are informational only. '
            'PRIME authority is broker-settled forward performance.',
            style: TextStyle(
              color: Colors.white38,
              fontSize: 8.8,
              height: 1.35,
            ),
          ),
        ],
      ),
    );
  }

  Widget _selectionCard(
    Map<String, dynamic> row,
  ) {
    final rank =
        row['category_rank'] ?? row['rank'] ?? '-';
    final market =
        '${row['market'] ?? row['name'] ?? row['symbol'] ?? '-'}';
    final symbol =
        '${row['symbol'] ?? row['key'] ?? '-'}';
    final direction =
        '${row['direction'] ?? 'WAIT'}'.toUpperCase();
    final strategy =
        '${row['strategy_name'] ?? row['strategy_id'] ?? '-'}';
    final regime =
        '${row['market_regime'] ?? row['regime'] ?? '-'}'
            .replaceAll('_', ' ');

    final quant =
        row['quant_confidence_pct'] != null
            ? _num(row['quant_confidence_pct'])
            : _pct(row['quant_confidence']);

    final ai = row[
                'model_ai_directional_confidence_pct'] !=
            null
        ? _num(row[
            'model_ai_directional_confidence_pct'])
        : _pct(row['model_ai_confidence']);

    final fast = _num(
      row['live_fast_score'] ??
          row['smart_fast_score'],
    );

    final forward = _forward(row);
    final provenance = _provenance(row);

    final tradeClass =
        '${row['trade_class'] ?? 'OBSERVE'}'
            .toUpperCase();

    final state =
        '${forward['state'] ?? 'BOOTSTRAP'}'
            .toUpperCase();

    final reasonsRaw = row['rejection_reasons'];
    final reasons = reasonsRaw is List
        ? reasonsRaw
            .map((e) => '$e')
            .take(3)
            .join(' • ')
        : '';

    final source =
        '${provenance['analysis_price_source'] ?? row['analysis_price_source'] ?? '-'}';

    final quote =
        '${provenance['broker_quote_source'] ?? row['broker_quote_source'] ?? '-'}';

    return Padding(
      padding: const EdgeInsets.only(bottom: 9),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Colors.white.withValues(
            alpha: .04,
          ),
          borderRadius:
              BorderRadius.circular(20),
          border: Border.all(
            color: Colors.white.withValues(
              alpha: .075,
            ),
          ),
        ),
        child: Column(
          crossAxisAlignment:
              CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  width: 28,
                  height: 28,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color:
                        _blue.withValues(alpha: .12),
                    shape: BoxShape.circle,
                  ),
                  child: Text(
                    '#$rank',
                    style: const TextStyle(
                      color: _blue,
                      fontWeight: FontWeight.w900,
                      fontSize: 9,
                    ),
                  ),
                ),
                const SizedBox(width: 9),
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
                          fontSize: 13,
                        ),
                      ),
                      Text(
                        '$symbol • $regime',
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
                  color: _directionColor(
                    direction,
                  ),
                ),
                const SizedBox(width: 5),
                _pill(
                  tradeClass,
                  color: tradeClass == 'PRIME'
                      ? _amber
                      : tradeClass == 'STRONG'
                          ? _green
                          : Colors.white54,
                ),
              ],
            ),
            const SizedBox(height: 9),
            Text(
              strategy,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: Colors.white60,
                fontSize: 9,
                fontWeight: FontWeight.w700,
              ),
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
            const SizedBox(height: 8),
            Row(
              children: [
                _metric(
                  'WR',
                  '${_pct(forward['win_rate']).toStringAsFixed(1)}%',
                ),
                _metric(
                  'PF',
                  _num(forward['profit_factor'])
                      .toStringAsFixed(2),
                ),
                _metric(
                  'EXP',
                  '${_num(forward['expectancy_r']) >= 0 ? '+' : ''}${_num(forward['expectancy_r']).toStringAsFixed(2)}R',
                ),
                _metric(
                  'STATE',
                  state,
                  color: state == 'PRIME'
                      ? _amber
                      : _blue,
                ),
              ],
            ),
            const SizedBox(height: 10),
            Text(
              'Analysis: $source • Broker: $quote',
              style: const TextStyle(
                color: Colors.white38,
                fontSize: 8.2,
              ),
            ),
            if (reasons.isNotEmpty) ...[
              const SizedBox(height: 5),
              Text(
                reasons,
                style: const TextStyle(
                  color: _amber,
                  fontSize: 8.2,
                  height: 1.3,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _loadingBanner() {
    if (!busy && error == null) {
      return const SizedBox.shrink();
    }

    return Padding(
      padding: const EdgeInsets.only(
        bottom: 10,
      ),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.white.withValues(
            alpha: .04,
          ),
          borderRadius:
              BorderRadius.circular(16),
          border: Border.all(
            color: Colors.white.withValues(
              alpha: .07,
            ),
          ),
        ),
        child: Row(
          children: [
            if (busy)
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
                color: _amber,
                size: 16,
              ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                error ??
                    'Loading ${loadingStep ?? selected}…',
                style: TextStyle(
                  color: error == null
                      ? Colors.white54
                      : _amber,
                  fontSize: 9,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _timeLabel() {
    final value = lastUpdated;
    if (value == null) return 'waiting';
    final h =
        value.hour.toString().padLeft(2, '0');
    final m =
        value.minute.toString().padLeft(2, '0');
    final s =
        value.second.toString().padLeft(2, '0');
    return '$h:$m:$s';
  }

  @override
  Widget build(BuildContext context) {
    final sourceMap =
        dataHealth['last_source_by_market'];
    final sourceCount =
        sourceMap is Map ? sourceMap.length : 0;

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
          Row(
            children: [
              const Expanded(
                child: Text(
                  'MARKETS — LIVE SPECIALISTS',
                  style: TextStyle(
                    color: _teal,
                    fontSize: 15,
                    fontWeight: FontWeight.w900,
                  ),
                ),
              ),
              Text(
                'updated ${_timeLabel()}',
                style: const TextStyle(
                  color: Colors.white38,
                  fontSize: 8.5,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          _loadingBanner(),
          SizedBox(
            height: 38,
            child: ListView.separated(
              scrollDirection: Axis.horizontal,
              itemCount: categories.length,
              separatorBuilder: (_, __) =>
                  const SizedBox(width: 7),
              itemBuilder: (_, index) {
                final category =
                    categories[index];
                final active =
                    category == selected;

                return ChoiceChip(
                  label: Text(category),
                  selected: active,
                  onSelected: (_) =>
                      selectCategory(category),
                  labelStyle: TextStyle(
                    fontSize: 9,
                    fontWeight: FontWeight.w800,
                    color: active
                        ? Colors.black
                        : Colors.white60,
                  ),
                  selectedColor: _teal,
                  backgroundColor:
                      Colors.white.withValues(
                    alpha: .04,
                  ),
                  side: BorderSide(
                    color: active
                        ? _teal
                        : Colors.white12,
                  ),
                );
              },
            ),
          ),
          const SizedBox(height: 10),
          _summaryCard(),
          const SizedBox(height: 8),
          Row(
            children: [
              _pill(
                'DATA SOURCES $sourceCount',
                color: sourceCount > 0
                    ? _green
                    : _blue,
              ),
              const SizedBox(width: 6),
              _pill(
                dataHealth[
                            'yahoo_cooldown_active'] ==
                        true
                    ? 'YAHOO COOLDOWN'
                    : 'ROUTER READY',
                color: dataHealth[
                            'yahoo_cooldown_active'] ==
                        true
                    ? _amber
                    : _green,
              ),
            ],
          ),
          const SizedBox(height: 12),
          if (selections.isEmpty && busy)
            Container(
              padding:
                  const EdgeInsets.all(28),
              alignment: Alignment.center,
              child: const Column(
                children: [
                  CircularProgressIndicator(
                    strokeWidth: 2,
                  ),
                  SizedBox(height: 10),
                  Text(
                    'Loading selected market rankings…',
                    style: TextStyle(
                      color: Colors.white54,
                      fontSize: 10,
                    ),
                  ),
                ],
              ),
            )
          else if (selections.isEmpty)
            Container(
              padding:
                  const EdgeInsets.all(28),
              alignment: Alignment.center,
              child: const Text(
                'No market selections returned for this category.',
                style: TextStyle(
                  color: Colors.white38,
                  fontSize: 10,
                ),
              ),
            )
          else
            ...selections.map(_selectionCard),
        ],
      ),
    );
  }
}
