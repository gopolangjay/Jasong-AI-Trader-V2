import 'dart:convert';

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

void main() => runApp(const JasongApp());

class JasongApp extends StatelessWidget {
  const JasongApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Jasong AI Trader',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        useMaterial3: true,
        colorSchemeSeed: Colors.teal,
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final symbol = TextEditingController(
    text: 'EURUSD=X',
  );

  final balance = TextEditingController(
    text: '10000',
  );

  String risk = 'Balanced';

  String apiBase = const String.fromEnvironment(
    'API_BASE_URL',
    defaultValue:
        'https://jasong-ai-trader-v2.onrender.com',
  );

  Map<String, dynamic>? sig;
  Map<String, dynamic>? bt;

  Map<String, dynamic>? fastScan;

  bool busy = false;
  bool scanningMarkets = false;

  String? error;

  // =========================================================
  // HTTP GET
  // =========================================================

  Future<Map<String, dynamic>> getJson(
    Uri uri, {
    int timeoutSeconds = 90,
  }) async {
    final response = await http
        .get(uri)
        .timeout(
          Duration(
            seconds: timeoutSeconds,
          ),
        );

    if (response.statusCode != 200) {
      throw Exception(
        'Server ${response.statusCode}: '
        '${response.body}',
      );
    }

    final decoded = jsonDecode(
      response.body,
    );

    if (decoded is! Map) {
      throw Exception(
        'Invalid server response',
      );
    }

    return Map<String, dynamic>.from(
      decoded,
    );
  }

  // =========================================================
  // LIVE AI SIGNAL
  // =========================================================

  Future<void> refreshSignal() async {
    if (busy) return;

    setState(() {
      busy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/signal',
      ).replace(
        queryParameters: {
          'symbol': symbol.text.trim(),
          'risk_mode': risk,
          'balance': balance.text.trim(),
        },
      );

      final result = await getJson(uri);

      if (!mounted) return;

      setState(() {
        sig = result;
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error = e.toString();
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;
        });
      }
    }
  }

  // =========================================================
  // BACKTEST
  // =========================================================

  Future<void> runBacktest() async {
    if (busy) return;

    setState(() {
      busy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/backtest',
      ).replace(
        queryParameters: {
          'symbol': symbol.text.trim(),
          'risk_mode': risk,
          'starting_balance':
              balance.text.trim(),
        },
      );

      final result = await getJson(uri);

      if (!mounted) return;

      setState(() {
        bt = result;
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error = e.toString();
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;
        });
      }
    }
  }

  // =========================================================
  // V4.2 FAST MARKET SCANNER
  // =========================================================

  Future<void> scanAllMarkets() async {
    if (busy) return;

    setState(() {
      busy = true;
      scanningMarkets = true;
      error = null;
      fastScan = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/fast-scan',
      ).replace(
        queryParameters: {
          'period': '5d',
          'interval': '15m',
          'top_n': '3',
        },
      );

      final result = await getJson(
        uri,
        timeoutSeconds: 90,
      );

      if (!mounted) return;

      setState(() {
        fastScan = result;
      });
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error =
            'Fast market scan failed: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;
          scanningMarkets = false;
        });
      }
    }
  }

  // =========================================================
  // LOAD MARKET INTO LIVE SIGNAL
  // =========================================================

  Future<void> analyseMarket(
    Map<String, dynamic> market,
  ) async {
    final marketSymbol =
        market['symbol']?.toString();

    if (marketSymbol == null ||
        marketSymbol.isEmpty) {
      return;
    }

    setState(() {
      symbol.text = marketSymbol;
      error = null;
    });

    await refreshSignal();
  }

  // =========================================================
  // BEST MARKET -> LIVE SIGNAL
  // =========================================================

  Future<void> analyseBestMarket() async {
    final best =
        fastScan?['best_candidate'];

    if (best is! Map) {
      return;
    }

    await analyseMarket(
      Map<String, dynamic>.from(best),
    );
  }

  // =========================================================
  // PAPER TRADE
  // =========================================================

  Future<void> recordPaperTrade() async {
    if (busy) return;

    final decision =
        sig?['decision']?.toString();

    if (decision != 'BUY' &&
        decision != 'SELL') {
      return;
    }

    setState(() {
      busy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/paper-trades',
      ).replace(
        queryParameters: {
          'symbol': symbol.text.trim(),
          'direction':
              sig!['decision'].toString(),
          'confidence':
              sig!['confidence'].toString(),
          'entry_price':
              sig!['price'].toString(),
          'stake':
              sig!['suggested_paper_stake']
                  .toString(),
        },
      );

      final response = await http
          .post(uri)
          .timeout(
            const Duration(
              seconds: 30,
            ),
          );

      if (response.statusCode != 200) {
        throw Exception(
          response.body,
        );
      }

      if (!mounted) return;

      ScaffoldMessenger.of(context)
          .showSnackBar(
        const SnackBar(
          content: Text(
            'Paper trade recorded',
          ),
        ),
      );
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error = e.toString();
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;
        });
      }
    }
  }

  // =========================================================
  // INIT / DISPOSE
  // =========================================================

  @override
  void initState() {
    super.initState();

    Future.microtask(
      refreshSignal,
    );
  }

  @override
  void dispose() {
    symbol.dispose();
    balance.dispose();

    super.dispose();
  }

  // =========================================================
  // COLOURS
  // =========================================================

  Color decisionColor(
    String decision,
  ) {
    if (decision == 'BUY') {
      return Colors.greenAccent;
    }

    if (decision == 'SELL') {
      return Colors.redAccent;
    }

    return Colors.amberAccent;
  }

  Color statusColor(
    String status,
  ) {
    switch (status) {
      case 'STRONG':
        return Colors.greenAccent;

      case 'QUALIFIED':
        return Colors.tealAccent;

      case 'WATCH':
        return Colors.amberAccent;

      case 'REJECT':
        return Colors.redAccent;

      case 'ERROR':
        return Colors.redAccent;

      default:
        return Colors.white70;
    }
  }

  // =========================================================
  // FORMATTERS
  // =========================================================

  String formatPrice(
    dynamic value,
  ) {
    if (value is num) {
      final number =
          value.toDouble();

      if (number >= 100) {
        return number
            .toStringAsFixed(3);
      }

      return number
          .toStringAsFixed(5);
    }

    return '-';
  }

  String formatNumber(
    dynamic value, {
    int decimals = 2,
  }) {
    if (value is num) {
      return value
          .toDouble()
          .toStringAsFixed(
            decimals,
          );
    }

    return '-';
  }

  String formatPercent(
    dynamic value, {
    int decimals = 1,
  }) {
    if (value is num) {
      return (
        value.toDouble() * 100
      ).toStringAsFixed(
        decimals,
      );
    }

    return '0.0';
  }

  // =========================================================
  // GENERIC METRIC CARD
  // =========================================================

  Widget metric(
    String label,
    String value,
  ) {
    return Expanded(
      child: Card(
        child: Padding(
          padding:
              const EdgeInsets.all(14),
          child: Column(
            children: [
              Text(
                label,
                style:
                    const TextStyle(
                  fontSize: 12,
                ),
              ),
              const SizedBox(
                height: 6,
              ),
              Text(
                value,
                textAlign:
                    TextAlign.center,
                style:
                    const TextStyle(
                  fontSize: 19,
                  fontWeight:
                      FontWeight.bold,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // =========================================================
  // FAST SCANNER MARKET CARD
  // =========================================================

  Widget fastMarketCard(
    Map<String, dynamic> market,
    int rank,
  ) {
    final marketName =
        market['market']
                ?.toString() ??
            '-';

    final direction =
        market['direction']
                ?.toString() ??
            'WAIT';

    final status =
        market['status']
                ?.toString() ??
            '-';

    final score =
        ((market['fast_score'] ?? 0)
                as num)
            .toDouble();

    final rsi =
        market['rsi'];

    final price =
        market['price'];

    final reasons =
        (market['reasons']
                    as List?) ??
            const [];

    return Card(
      child: InkWell(
        borderRadius:
            BorderRadius.circular(12),
        onTap: busy
            ? null
            : () {
                analyseMarket(
                  market,
                );
              },
        child: Padding(
          padding:
              const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment:
                CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      '#$rank $marketName',
                      style:
                          const TextStyle(
                        fontSize: 20,
                        fontWeight:
                            FontWeight.bold,
                      ),
                    ),
                  ),
                  Text(
                    direction,
                    style:
                        TextStyle(
                      fontSize: 18,
                      fontWeight:
                          FontWeight.bold,
                      color:
                          decisionColor(
                        direction,
                      ),
                    ),
                  ),
                ],
              ),

              const SizedBox(
                height: 6,
              ),

              Row(
                children: [
                  Text(
                    status,
                    style:
                        TextStyle(
                      fontWeight:
                          FontWeight.bold,
                      color:
                          statusColor(
                        status,
                      ),
                    ),
                  ),
                  const Spacer(),
                  Text(
                    'Score '
                    '${score.toStringAsFixed(1)}',
                    style:
                        const TextStyle(
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                ],
              ),

              const SizedBox(
                height: 6,
              ),

              Text(
                'Price '
                '${formatPrice(price)}'
                ' • RSI '
                '${formatNumber(rsi)}',
              ),

              const SizedBox(
                height: 4,
              ),

              Text(
                '${market['interval'] ?? '-'}'
                ' • '
                '${market['period'] ?? '-'}',
              ),

              if (reasons.isNotEmpty) ...[
                const SizedBox(
                  height: 6,
                ),
                Text(
                  reasons
                      .take(3)
                      .join(' • '),
                  style:
                      const TextStyle(
                    fontSize: 12,
                    color:
                        Colors.white70,
                  ),
                ),
              ],

              const SizedBox(
                height: 6,
              ),

              const Text(
                'Tap to load live AI signal',
                style:
                    TextStyle(
                  fontSize: 11,
                  color:
                      Colors.tealAccent,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  // =========================================================
  // MAIN UI
  // =========================================================

  @override
  Widget build(
    BuildContext context,
  ) {
    final decision =
        sig?['decision']
                ?.toString() ??
            'WAIT';

    final confidence =
        formatPercent(
      sig?['confidence'],
    );

    final aiUp =
        formatPercent(
      sig?[
          'combined_up_probability'],
    );

    final priceText =
        formatPrice(
      sig?['price'],
    );

    final rsiText =
        formatNumber(
      sig?['rsi'],
    );

    final curve =
        (bt?['equity_curve']
                    as List?) ??
            const [];

    final topCandidates =
        (fastScan?[
                    'top_candidates']
                as List?) ??
            const [];

    final ranking =
        (fastScan?['ranking']
                    as List?) ??
            const [];

    final dynamic bestCandidate =
        fastScan?[
            'best_candidate'];

    final marketsTested =
        fastScan?[
                'markets_tested'] ??
            0;

    final marketsSuccessful =
        fastScan?[
                'markets_successful'] ??
            0;

    final marketsFailed =
        fastScan?[
                'markets_failed'] ??
            0;

    final candidatesFound =
        fastScan?[
                'candidates_found'] ??
            0;

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'Jasong AI Trader V4.2',
        ),
        actions: [
          IconButton(
            onPressed:
                busy
                    ? null
                    : refreshSignal,
            icon:
                const Icon(
              Icons.refresh,
            ),
          ),
        ],
      ),

      body: RefreshIndicator(
        onRefresh:
            refreshSignal,

        child: ListView(
          physics:
              const AlwaysScrollableScrollPhysics(),

          padding:
              const EdgeInsets.all(14),

          children: [
            // =================================================
            // LIVE SIGNAL SECTION
            // =================================================

            const Text(
              'AI-assisted paper trading',
              style:
                  TextStyle(
                fontWeight:
                    FontWeight.bold,
              ),
            ),

            const SizedBox(
              height: 10,
            ),

            TextField(
              controller:
                  symbol,
              decoration:
                  const InputDecoration(
                labelText:
                    'Market symbol',
                border:
                    OutlineInputBorder(),
              ),
            ),

            const SizedBox(
              height: 10,
            ),

            TextField(
              controller:
                  balance,
              keyboardType:
                  const TextInputType
                      .numberWithOptions(
                decimal: true,
              ),
              decoration:
                  const InputDecoration(
                labelText:
                    'Paper balance',
                border:
                    OutlineInputBorder(),
              ),
            ),

            const SizedBox(
              height: 10,
            ),

            DropdownButtonFormField<
                String>(
              initialValue:
                  risk,

              decoration:
                  const InputDecoration(
                labelText:
                    'Risk mode',
                border:
                    OutlineInputBorder(),
              ),

              items: [
                'Conservative',
                'Balanced',
                'Aggressive',
              ]
                  .map(
                    (item) =>
                        DropdownMenuItem<
                            String>(
                      value: item,
                      child:
                          Text(item),
                    ),
                  )
                  .toList(),

              onChanged:
                  busy
                      ? null
                      : (value) {
                          setState(
                            () {
                              risk =
                                  value ??
                                      'Balanced';
                            },
                          );
                        },
            ),

            const SizedBox(
              height: 14,
            ),

            Card(
              child: Padding(
                padding:
                    const EdgeInsets.all(
                  18,
                ),
                child: Column(
                  children: [
                    Text(
                      decision,
                      style:
                          TextStyle(
                        fontSize: 44,
                        fontWeight:
                            FontWeight.w900,
                        color:
                            decisionColor(
                          decision,
                        ),
                      ),
                    ),

                    const SizedBox(
                      height: 6,
                    ),

                    Text(
                      sig?['reason']
                              ?.toString() ??
                          'Waiting for signal...',
                      textAlign:
                          TextAlign.center,
                    ),
                  ],
                ),
              ),
            ),

            Row(
              children: [
                metric(
                  'Confidence',
                  '$confidence%',
                ),
                metric(
                  'AI up',
                  '$aiUp%',
                ),
              ],
            ),

            Row(
              children: [
                metric(
                  'Price',
                  priceText,
                ),
                metric(
                  'RSI',
                  rsiText,
                ),
              ],
            ),

            Row(
              children: [
                metric(
                  'Paper stake',
                  '${sig?['suggested_paper_stake'] ?? '-'}',
                ),
                metric(
                  'Mode',
                  risk,
                ),
              ],
            ),

            const SizedBox(
              height: 10,
            ),

            FilledButton.icon(
              onPressed:
                  busy
                      ? null
                      : refreshSignal,
              icon:
                  const Icon(
                Icons.psychology,
              ),
              label:
                  const Text(
                'Refresh AI Signal',
              ),
            ),

            const SizedBox(
              height: 8,
            ),

            OutlinedButton.icon(
              onPressed:
                  busy
                      ? null
                      : runBacktest,
              icon:
                  const Icon(
                Icons.query_stats,
              ),
              label:
                  const Text(
                'Run Backtest',
              ),
            ),

            const SizedBox(
              height: 8,
            ),

            FilledButton.icon(
              onPressed:
                  busy
                      ? null
                      : scanAllMarkets,
              icon:
                  const Icon(
                Icons.radar,
              ),
              label:
                  const Text(
                'Fast Scan All Markets',
              ),
            ),

            const SizedBox(
              height: 8,
            ),

            OutlinedButton.icon(
              onPressed:
                  busy ||
                          ![
                            'BUY',
                            'SELL',
                          ].contains(
                            decision,
                          )
                      ? null
                      : recordPaperTrade,
              icon:
                  const Icon(
                Icons.edit_note,
              ),
              label:
                  const Text(
                'Record Paper Trade',
              ),
            ),

            // =================================================
            // LOADING
            // =================================================

            if (
              busy &&
              !scanningMarkets
            )
              const Padding(
                padding:
                    EdgeInsets.all(
                  16,
                ),
                child:
                    Center(
                  child:
                      CircularProgressIndicator(),
                ),
              ),

            if (scanningMarkets)
              const Padding(
                padding:
                    EdgeInsets.all(
                  16,
                ),
                child: Column(
                  children: [
                    LinearProgressIndicator(),
                    SizedBox(
                      height: 10,
                    ),
                    Text(
                      'Scanning 9 markets...',
                    ),
                    SizedBox(
                      height: 4,
                    ),
                    Text(
                      'EMA • RSI • MACD • Momentum • Volatility',
                      style:
                          TextStyle(
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),

            // =================================================
            // ERRORS
            // =================================================

            if (error != null)
              Padding(
                padding:
                    const EdgeInsets.only(
                  top: 10,
                ),
                child: Text(
                  error!,
                  style:
                      const TextStyle(
                    color:
                        Colors.redAccent,
                  ),
                ),
              ),

            // =================================================
            // FAST SCANNER RESULTS
            // =================================================

            if (fastScan != null) ...[
              const SizedBox(
                height: 20,
              ),

              const Divider(),

              const SizedBox(
                height: 10,
              ),

              Row(
                children: [
                  const Icon(
                    Icons.bolt,
                  ),
                  const SizedBox(
                    width: 8,
                  ),
                  const Text(
                    'V4.2 Fast Market Scanner',
                    style:
                        TextStyle(
                      fontSize: 22,
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                ],
              ),

              const SizedBox(
                height: 12,
              ),

              Row(
                children: [
                  metric(
                    'Scanned',
                    '$marketsTested',
                  ),
                  metric(
                    'Successful',
                    '$marketsSuccessful',
                  ),
                ],
              ),

              Row(
                children: [
                  metric(
                    'Candidates',
                    '$candidatesFound',
                  ),
                  metric(
                    'Failures',
                    '$marketsFailed',
                  ),
                ],
              ),

              // ===============================================
              // BEST OPPORTUNITY
              // ===============================================

              if (bestCandidate != null) ...[
                const SizedBox(
                  height: 12,
                ),

                Card(
                  child: Padding(
                    padding:
                        const EdgeInsets.all(
                      18,
                    ),
                    child: Column(
                      children: [
                        const Text(
                          'BEST OPPORTUNITY',
                          style:
                              TextStyle(
                            fontSize: 14,
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),

                        const SizedBox(
                          height: 10,
                        ),

                        Text(
                          bestCandidate[
                                  'market']
                              .toString(),
                          style:
                              const TextStyle(
                            fontSize: 34,
                            fontWeight:
                                FontWeight.w900,
                          ),
                        ),

                        const SizedBox(
                          height: 6,
                        ),

                        Text(
                          bestCandidate[
                                  'direction']
                              .toString(),
                          style:
                              TextStyle(
                            fontSize: 30,
                            fontWeight:
                                FontWeight.w900,
                            color:
                                decisionColor(
                              bestCandidate[
                                      'direction']
                                  .toString(),
                            ),
                          ),
                        ),

                        const SizedBox(
                          height: 10,
                        ),

                        Row(
                          children: [
                            metric(
                              'Fast score',
                              '${bestCandidate['fast_score'] ?? '-'}',
                            ),
                            metric(
                              'RSI',
                              formatNumber(
                                bestCandidate[
                                    'rsi'],
                              ),
                            ),
                          ],
                        ),

                        Row(
                          children: [
                            metric(
                              'Price',
                              formatPrice(
                                bestCandidate[
                                    'price'],
                              ),
                            ),
                            metric(
                              'Status',
                              '${bestCandidate['status'] ?? '-'}',
                            ),
                          ],
                        ),

                        const SizedBox(
                          height: 10,
                        ),

                        FilledButton.icon(
                          onPressed:
                              busy
                                  ? null
                                  : analyseBestMarket,
                          icon:
                              const Icon(
                            Icons.psychology,
                          ),
                          label:
                              const Text(
                            'Analyse Best Market',
                          ),
                        ),

                        const SizedBox(
                          height: 8,
                        ),

                        const Text(
                          'Fast score is a ranking score, '
                          'not a guaranteed win probability.',
                          textAlign:
                              TextAlign.center,
                          style:
                              TextStyle(
                            fontSize: 11,
                            color:
                                Colors.white70,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],

              // ===============================================
              // TOP 3
              // ===============================================

              const SizedBox(
                height: 16,
              ),

              const Text(
                'Top 3 Markets',
                style:
                    TextStyle(
                  fontSize: 20,
                  fontWeight:
                      FontWeight.bold,
                ),
              ),

              const SizedBox(
                height: 8,
              ),

              if (topCandidates.isEmpty)
                const Card(
                  child: Padding(
                    padding:
                        EdgeInsets.all(
                      16,
                    ),
                    child: Text(
                      'No candidates available.',
                    ),
                  ),
                ),

              for (
                int i = 0;
                i <
                    topCandidates.length;
                i++
              )
                fastMarketCard(
                  Map<
                      String,
                      dynamic
                  >.from(
                    topCandidates[i],
                  ),
                  i + 1,
                ),

              // ===============================================
              // FULL RANKING
              // ===============================================

              if (ranking.isNotEmpty) ...[
                const SizedBox(
                  height: 16,
                ),

                ExpansionTile(
                  title:
                      const Text(
                    'Full Market Ranking',
                    style:
                        TextStyle(
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                  children: [
                    for (
                      int i = 0;
                      i <
                          ranking.length;
                      i++
                    )
                      ListTile(
                        leading:
                            CircleAvatar(
                          child: Text(
                            '${i + 1}',
                          ),
                        ),
                        title:
                            Text(
                          '${ranking[i]['market']}',
                        ),
                        subtitle:
                            Text(
                          '${ranking[i]['direction']}'
                          ' • '
                          '${ranking[i]['status']}',
                        ),
                        trailing:
                            Text(
                          '${ranking[i]['fast_score']}',
                          style:
                              const TextStyle(
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),
                        onTap:
                            busy
                                ? null
                                : () {
                                    analyseMarket(
                                      Map<
                                          String,
                                          dynamic
                                      >.from(
                                        ranking[i],
                                      ),
                                    );
                                  },
                      ),
                  ],
                ),
              ],
            ],

            // =================================================
            // BACKTEST SECTION
            // =================================================

            if (bt != null) ...[
              const SizedBox(
                height: 20,
              ),

              const Divider(),

              const SizedBox(
                height: 10,
              ),

              const Text(
                'Backtest',
                style:
                    TextStyle(
                  fontSize: 20,
                  fontWeight:
                      FontWeight.bold,
                ),
              ),

              Row(
                children: [
                  metric(
                    'Trades',
                    '${bt!['trades']}',
                  ),
                  metric(
                    'Win rate',
                    '${formatPercent(bt!['win_rate'])}%',
                  ),
                ],
              ),

              Row(
                children: [
                  metric(
                    'Return',
                    '${formatPercent(bt!['return_pct'])}%',
                  ),
                  metric(
                    'Max DD',
                    '${formatPercent(bt!['max_drawdown'])}%',
                  ),
                ],
              ),

              if (curve.isNotEmpty)
                SizedBox(
                  height: 220,
                  child:
                      LineChart(
                    LineChartData(
                      titlesData:
                          const FlTitlesData(
                        show: false,
                      ),
                      borderData:
                          FlBorderData(
                        show: true,
                      ),
                      lineBarsData: [
                        LineChartBarData(
                          isCurved: true,
                          dotData:
                              const FlDotData(
                            show: false,
                          ),
                          spots: [
                            for (
                              int i = 0;
                              i <
                                  curve.length;
                              i++
                            )
                              FlSpot(
                                i.toDouble(),
                                ((curve[i]
                                                as Map)[
                                            'balance']
                                        as num)
                                    .toDouble(),
                              ),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
            ],

            const SizedBox(
              height: 16,
            ),

            // =================================================
            // SAFETY
            // =================================================

            const Card(
              child: Padding(
                padding:
                    EdgeInsets.all(
                  14,
                ),
                child: Text(
                  'Safety: no Martingale, '
                  'no forced daily-profit target, '
                  'no live broker execution, and no '
                  'broker password stored in the app. '
                  'Fast scores, historical/model results '
                  'and market rankings do not guarantee '
                  'future profit.',
                ),
              ),
            ),

            const SizedBox(
              height: 30,
            ),
          ],
        ),
      ),
    );
  }
}
