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
  Map<String, dynamic>? optimizerStatus;
  Map<String, dynamic>? fullRefreshStatus;
  List<Map<String, dynamic>> selections = const [];
  bool busy = false;
  String? error;
  Timer? fullRefreshPollTimer;

  @override
  void initState() {
    super.initState();
    refreshAll();
  }

  @override
  void dispose() {
    fullRefreshPollTimer?.cancel();
    super.dispose();
  }

  Future<Map<String, dynamic>> _get(String path) async {
    final response = await http
        .get(
          Uri.parse('${widget.apiBase}$path'),
          headers: const {'Accept': 'application/json'},
        )
        .timeout(const Duration(seconds: 45));
    if (response.statusCode != 200) {
      throw HttpException('HTTP ${response.statusCode}: ${response.body}');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Unexpected backend response');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<Map<String, dynamic>> _post(String path) async {
    final response = await http
        .post(
          Uri.parse('${widget.apiBase}$path'),
          headers: const {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
          },
          body: '{}',
        )
        .timeout(const Duration(seconds: 120));
    if (response.statusCode != 200) {
      throw HttpException('HTTP ${response.statusCode}: ${response.body}');
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

  Future<void> refreshAll({bool silent = false}) async {
    if (busy && !silent) return;
    if (!silent && mounted) {
      setState(() {
        busy = true;
        error = null;
      });
    }
    try {
      final results = await Future.wait([
        _get('/market-categories/status'),
        _get('/market-categories/$selected'),
        _get('/category-portfolio/status'),
        _get('/market-categories/optimizer'),
        _get('/market-categories/full-refresh'),
      ]);
      if (!mounted) return;
      setState(() {
        systemStatus = results[0];
        selections = _mapList(results[1]['selections']);
        portfolioStatus = results[2];
        optimizerStatus = results[3];
        fullRefreshStatus = results[4];
        error = null;
      });
      final refreshState = '${results[4]['status'] ?? ''}'.toUpperCase();
      if (refreshState != 'RUNNING') {
        fullRefreshPollTimer?.cancel();
      }
    } catch (e) {
      if (!mounted || silent) return;
      setState(() => error = e.toString());
    } finally {
      if (!silent && mounted) setState(() => busy = false);
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

  Future<void> scanSelected() async {
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await _post('/market-categories/$selected/run-now');
      await refreshAll(silent: true);
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> scanAll() async {
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await _post('/market-categories/run-now');
      await refreshAll(silent: true);
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> optimiseAll40() async {
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await _post('/market-categories/full-refresh');
      await refreshAll(silent: true);
      fullRefreshPollTimer?.cancel();
      fullRefreshPollTimer = Timer.periodic(
        const Duration(seconds: 8),
        (_) => refreshAll(silent: true),
      );
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<void> runPortfolio() async {
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await _post('/category-portfolio/run-now');
      await refreshAll(silent: true);
    } catch (e) {
      if (mounted) setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  double _num(dynamic value) {
    if (value is num) return value.toDouble();
    return double.tryParse('$value') ?? 0.0;
  }

  Widget _pill(String text, {Color? color}) {
    final c = color ?? const Color(0xFF6FA8FF);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
      decoration: BoxDecoration(
        color: c.withValues(alpha: .12),
        border: Border.all(color: c.withValues(alpha: .35)),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: c,
          fontSize: 10,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }

  Color _directionColor(String direction) {
    if (direction == 'BUY') return const Color(0xFF67F0C1);
    if (direction == 'SELL') return const Color(0xFFFF7E8B);
    return const Color(0xFFFFD75E);
  }

  Widget _summaryCard() {
    final categoriesData = systemStatus?['categories'];
    final categoryData = categoriesData is Map ? categoriesData[selected] : null;
    final data = categoryData is Map
        ? Map<String, dynamic>.from(categoryData)
        : <String, dynamic>{};
    final standardReady = data['standard_ready'] ?? 0;
    final compoundReady = data['compound_ready'] ?? 0;
    final strategy = '${data['strategy'] ?? '-'}';
    final openByCategory = portfolioStatus?['open_by_category'];
    final open = openByCategory is Map ? (openByCategory[selected] ?? 0) : 0;
    final evidence = systemStatus?['evidence_hygiene'];
    final evidenceMap = evidence is Map
        ? Map<String, dynamic>.from(evidence)
        : <String, dynamic>{};
    final optimised = evidenceMap['markets_optimised'] ?? 0;
    final pending = evidenceMap['markets_pending_optimisation'] ?? 40;
    final refreshState = '${fullRefreshStatus?['status'] ?? 'PENDING'}';

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
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
                      '$selected AI',
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w900,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      strategy,
                      style: const TextStyle(color: Colors.white60, fontSize: 11),
                    ),
                  ],
                ),
              ),
              _pill('28 / 40 AI', color: const Color(0xFF65E6D3)),
            ],
          ),
          const SizedBox(height: 14),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _pill('$standardReady standard ready'),
              _pill('$compoundReady compound ready', color: const Color(0xFFFFD75E)),
              _pill('$open open', color: const Color(0xFFB899FF)),
              _pill('$optimised / 40 optimised', color: const Color(0xFF67F0C1)),
              _pill('$pending pending', color: pending == 0 ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
              _pill('refresh $refreshState', color: const Color(0xFFB899FF)),
              _pill('70% + 3-fold evidence', color: const Color(0xFF67F0C1)),
            ],
          ),
        ],
      ),
    );
  }

  Widget _selectionCard(Map<String, dynamic> item) {
    final rank = item['category_rank'] ?? '-';
    final symbol = '${item['market'] ?? item['symbol'] ?? '-'}';
    final direction = '${item['direction'] ?? 'WAIT'}'.toUpperCase();
    final quant = _num(item['quant_confidence_pct']);
    final ai = _num(item['model_ai_directional_confidence_pct']);
    final wr = _num(item['historical_win_rate_pct']);
    final pf = _num(item['historical_profit_factor']);
    final fast = _num(item['smart_fast_score']);
    final standard = item['standard_eligible'] == true;
    final compoundSlot = item['compound_slot_candidate'] == true;
    final compound = item['compound_eligible'] == true;
    final verified70 = item['historical_70_verified'] == true;
    final optimizerComplete = item['optimizer_complete'] == true;
    final selectionStable = item['optimizer_selection_stable'] == true;
    final walkForward = item['walk_forward_pass'] == true;
    final wfMedian = _num(item['walk_forward_median_win_rate_pct']);
    final tradeable = item['ig_tradeable'] == true;
    final regime = '${item['regime'] ?? '-'}'.replaceAll('_', ' ');
    final strategy = '${item['strategy_name'] ?? '-'}';

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(15),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .04),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(
          color: compound
              ? const Color(0xFFFFD75E).withValues(alpha: .38)
              : Colors.white.withValues(alpha: .07),
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
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      symbol,
                      style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 15),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      '$regime • $strategy',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: Colors.white54, fontSize: 10),
                    ),
                  ],
                ),
              ),
              _pill(direction, color: _directionColor(direction)),
            ],
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 7,
            runSpacing: 7,
            children: [
              _pill('28 AI ${quant.toStringAsFixed(1)}%', color: quant >= 28 ? const Color(0xFF67F0C1) : const Color(0xFFFF7E8B)),
              _pill('40 AI ${ai.toStringAsFixed(1)}%', color: ai >= 40 ? const Color(0xFF67F0C1) : const Color(0xFFFF7E8B)),
              _pill('WR ${wr.toStringAsFixed(1)}%', color: verified70 ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
              _pill('PF ${pf.toStringAsFixed(2)}'),
              _pill('Fast ${fast.toStringAsFixed(0)}'),
              _pill(optimizerComplete ? 'OPTIMISED' : 'PENDING OPT', color: optimizerComplete ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
              _pill(selectionStable ? 'SELECTION STABLE' : 'SELECTION UNSTABLE', color: selectionStable ? const Color(0xFF67F0C1) : const Color(0xFFFF7E8B)),
              _pill('WF median ${wfMedian.toStringAsFixed(1)}%', color: walkForward ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: Text(
                  standard ? 'STANDARD: TRADE' : 'STANDARD: NO TRADE',
                  style: TextStyle(
                    color: standard ? const Color(0xFF67F0C1) : Colors.white38,
                    fontSize: 10,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ),
              if (compoundSlot)
                Text(
                  compound ? '⚡ COMPOUND' : '⚡ SLOT / NOT QUALIFIED',
                  style: TextStyle(
                    color: compound ? const Color(0xFFFFD75E) : Colors.white38,
                    fontSize: 10,
                    fontWeight: FontWeight.w900,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 5),
          Text(
            '${tradeable ? 'IG DEMO tradeable' : 'IG DEMO not resolved/tradeable'} • ${walkForward ? 'walk-forward PASS' : 'walk-forward not passed'}',
            style: const TextStyle(color: Colors.white38, fontSize: 9),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: refreshAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 120),
        children: [
          const Text(
            'V6.9.2 SPECIALIST REAL-TIME TESTING',
            style: TextStyle(fontSize: 19, fontWeight: FontWeight.w900),
          ),
          const SizedBox(height: 4),
          const Text(
            'All 40 markets must complete current-schema optimisation and 3-fold chronological validation before Standard/Compound eligibility',
            style: TextStyle(color: Colors.white54, fontSize: 11, height: 1.35),
          ),
          const SizedBox(height: 14),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: categories
                  .map(
                    (category) => Padding(
                      padding: const EdgeInsets.only(right: 7),
                      child: ChoiceChip(
                        selected: selected == category,
                        label: Text(category),
                        onSelected: (_) => selectCategory(category),
                      ),
                    ),
                  )
                  .toList(),
            ),
          ),
          const SizedBox(height: 14),
          _summaryCard(),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: busy ? null : scanSelected,
                  icon: const Icon(Icons.radar_rounded),
                  label: Text('Scan $selected'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: busy ? null : scanAll,
                  icon: const Icon(Icons.public_rounded),
                  label: const Text('Scan Batch'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: FilledButton.tonalIcon(
              onPressed: busy ? null : optimiseAll40,
              icon: const Icon(Icons.model_training_rounded),
              label: const Text('Optimise / Refresh All 40 Markets'),
            ),
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: busy ? null : runPortfolio,
              icon: const Icon(Icons.play_circle_outline_rounded),
              label: const Text('Run IG DEMO Category Portfolio Now'),
            ),
          ),
          if (busy) ...[
            const SizedBox(height: 12),
            const LinearProgressIndicator(),
          ],
          if (error != null) ...[
            const SizedBox(height: 12),
            Text(
              error!,
              style: const TextStyle(color: Color(0xFFFF7E8B), fontSize: 11),
            ),
          ],
          const SizedBox(height: 18),
          Row(
            children: [
              const Expanded(
                child: Text(
                  'TOP SELECTIONS',
                  style: TextStyle(fontWeight: FontWeight.w900, fontSize: 13),
                ),
              ),
              _pill('${selections.length} / 5'),
            ],
          ),
          const SizedBox(height: 10),
          if (selections.isEmpty && !busy)
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: .035),
                borderRadius: BorderRadius.circular(16),
              ),
              child: const Text(
                'No fresh ranked selections yet. Run the category scan; the engine will not invent filler trades.',
                style: TextStyle(color: Colors.white54, fontSize: 11),
              ),
            )
          else
            ...selections.map(_selectionCard),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: const Color(0xFF65E6D3).withValues(alpha: .06),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: const Color(0xFF65E6D3).withValues(alpha: .16)),
            ),
            child: const Text(
              'Testing policy: 28% Quant and 40% directional Model-AI remain live gates. A strategy must also pass stable variant selection, genuine 70% aggregate held-out evidence, profit-factor/drawdown gates and 3 chronological validation folds before Standard/Compound execution. Real-time IG DEMO testing is enabled; live-money execution remains off.',
              style: TextStyle(color: Colors.white60, fontSize: 10, height: 1.45),
            ),
          ),
        ],
      ),
    );
  }
}
