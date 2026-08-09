import 'dart:async';
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

  List<Map<String, dynamic>> marketResults = [];
  List<Map<String, dynamic>> marketFailures = [];

  Map<String, dynamic>? rankedMarkets;

  int scanProgress = 0;

  bool busy = false;
  bool scanningMarkets = false;

  String? currentScanMarket;
  String? error;

  final List<String> markets = [
    'EURUSD',
    'GBPUSD',
    'USDJPY',
    'AUDUSD',
    'NZDUSD',
    'USDCAD',
    'USDCHF',
    'EURJPY',
    'GBPJPY',
  ];

  // =========================================================
  // BASIC GET REQUEST
  // =========================================================

  Future<Map<String, dynamic>> getJson(
    Uri uri,
  ) async {
    final response = await http
        .get(uri)
        .timeout(
          const Duration(seconds: 90),
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
  // BASIC POST JSON REQUEST
  // =========================================================

  Future<Map<String, dynamic>> postJson(
    Uri uri,
    dynamic body,
  ) async {
    final response = await http
        .post(
          uri,
          headers: {
            'Content-Type':
                'application/json',
          },
          body: jsonEncode(body),
        )
        .timeout(
          const Duration(seconds: 90),
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
  // V4.1 RESILIENT SEQUENTIAL MARKET SCANNER
  // =========================================================

  Future<void> scanAllMarkets() async {
    if (busy) {
      return;
    }

    setState(() {
      busy = true;
      scanningMarkets = true;

      error = null;

      marketResults = [];
      marketFailures = [];

      rankedMarkets = null;

      scanProgress = 0;
      currentScanMarket = null;
    });

    final completed =
        <Map<String, dynamic>>[];

    final failures =
        <Map<String, dynamic>>[];

    try {
      for (
        int i = 0;
        i < markets.length;
        i++
      ) {
        if (!mounted) {
          return;
        }

        final market = markets[i];

        setState(() {
          scanProgress = i + 1;
          currentScanMarket = market;
        });

        final uri = Uri.parse(
          '$apiBase/scan-market',
        ).replace(
          queryParameters: {
            'market': market,
            'risk_mode': risk,
            'starting_balance':
                balance.text,
            'payout': '0.80',
          },
        );

        try {
          // Each market has its own timeout.
          // A timeout no longer kills the
          // complete 9-market scan.
          final response = await http
              .get(uri)
              .timeout(
                const Duration(
                  seconds: 75,
                ),
              );

          if (response.statusCode != 200) {
            throw Exception(
              'HTTP '
              '${response.statusCode}',
            );
          }

          final decoded = jsonDecode(
            response.body,
          );

          if (decoded is! Map) {
            throw Exception(
              'Invalid market response',
            );
          }

          final result =
              Map<String, dynamic>.from(
            decoded,
          );

          result['market'] ??= market;

          completed.add(result);

          if (!mounted) {
            return;
          }

          setState(() {
            marketResults =
                List<
                    Map<String, dynamic>
                >.from(
              completed,
            );
          });
        } on TimeoutException {
          failures.add({
            'market': market,
            'status': 'TIMEOUT',
            'error':
                'Market scan exceeded '
                '75 seconds',
          });

          if (mounted) {
            setState(() {
              marketFailures =
                  List<
                      Map<String, dynamic>
                  >.from(
                failures,
              );
            });
          }
        } catch (e) {
          failures.add({
            'market': market,
            'status': 'ERROR',
            'error': e.toString(),
          });

          if (mounted) {
            setState(() {
              marketFailures =
                  List<
                      Map<String, dynamic>
                  >.from(
                failures,
              );
            });
          }
        }

        // Give Render a brief recovery
        // period before the next heavy
        // optimisation request.
        await Future.delayed(
          const Duration(seconds: 2),
        );
      }

      if (completed.isEmpty) {
        throw Exception(
          'No market scans completed '
          'successfully. Please try again.',
        );
      }

      if (!mounted) {
        return;
      }

      setState(() {
        currentScanMarket =
            'Ranking markets';
      });

      final rankUri = Uri.parse(
        '$apiBase/rank-markets',
      ).replace(
        queryParameters: {
          'top_n': '3',
        },
      );

      try {
        final ranked =
            await postJson(
          rankUri,
          completed,
        );

        if (!mounted) {
          return;
        }

        setState(() {
          rankedMarkets = ranked;
        });
      } catch (e) {
        if (!mounted) {
          return;
        }

        setState(() {
          error =
              'Market scans completed, '
              'but ranking failed: $e';
        });
      }
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error = e.toString();
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;

          scanningMarkets = false;

          scanProgress = 0;

          currentScanMarket = null;
        });
      }
    }
  }

  // =========================================================
  // LIVE AI SIGNAL
  // =========================================================

  Future<void> refreshSignal() async {
    if (busy) {
      return;
    }

    setState(() {
      busy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/signal',
      ).replace(
        queryParameters: {
          'symbol': symbol.text,
          'risk_mode': risk,
          'balance': balance.text,
        },
      );

      final result =
          await getJson(uri);

      if (!mounted) {
        return;
      }

      setState(() {
        sig = result;
      });
    } catch (e) {
      if (!mounted) {
        return;
      }

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
    if (busy) {
      return;
    }

    setState(() {
      busy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/backtest',
      ).replace(
        queryParameters: {
          'symbol': symbol.text,
          'risk_mode': risk,
          'starting_balance':
              balance.text,
        },
      );

      final result =
          await getJson(uri);

      if (!mounted) {
        return;
      }

      setState(() {
        bt = result;
      });
    } catch (e) {
      if (!mounted) {
        return;
      }

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
  // RECORD PAPER TRADE
  // =========================================================

  Future<void>
      recordPaperTrade() async {
    if (busy) {
      return;
    }

    if (sig == null ||
        ![
          'BUY',
          'SELL',
        ].contains(
          sig!['decision'],
        )) {
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
          'symbol': symbol.text,
          'direction':
              sig!['decision']
                  .toString(),
          'confidence':
              sig!['confidence']
                  .toString(),
          'entry_price':
              sig!['price']
                  .toString(),
          'stake':
              sig![
                      'suggested_paper_stake']
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

      if (!mounted) {
        return;
      }

      ScaffoldMessenger.of(context)
          .showSnackBar(
        const SnackBar(
          content: Text(
            'Paper trade recorded',
          ),
        ),
      );
    } catch (e) {
      if (!mounted) {
        return;
      }

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

      case 'TIMEOUT':
        return Colors.orangeAccent;

      case 'ERROR':
        return Colors.redAccent;

      default:
        return Colors.white70;
    }
  }

  // =========================================================
  // NUMBER FORMATTING
  // =========================================================

  String formatPrice(
    dynamic value,
  ) {
    if (value is num) {
      return value
          .toDouble()
          .toStringAsFixed(5);
    }

    return '-';
  }

  String formatRsi(
    dynamic value,
  ) {
    if (value is num) {
      return value
          .toDouble()
          .toStringAsFixed(2);
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
  // MARKET CARD
  // =========================================================

  Widget marketCard(
    Map<String, dynamic> market,
    int rank,
  ) {
    final winRate =
        ((market['win_rate'] ?? 0)
                    as num)
                .toDouble() *
            100;

    final score =
        ((market['market_score'] ?? 0)
                as num)
            .toDouble();

    final profitFactor =
        ((market['profit_factor'] ??
                    0)
                as num)
            .toDouble();

    final returnPct =
        ((market['return_pct'] ?? 0)
                    as num)
                .toDouble() *
            100;

    final status =
        market['status']
                ?.toString() ??
            'UNKNOWN';

    String holdText =
        '${market['holding_candles'] ?? '-'} '
        'candles';

    final interval =
        market['interval']
            ?.toString();

    final holdingCandles =
        market['holding_candles'];

    if (interval != null &&
        holdingCandles is num) {
      final holdMinutes =
          _holdingMinutes(
        interval,
        holdingCandles.toInt(),
      );

      if (holdMinutes != null) {
        holdText =
            '${holdingCandles.toInt()} '
            'candles • '
            '~${_formatMinutes(holdMinutes)}';
      }
    }

    return Card(
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
                    '#$rank '
                    '${market['market'] ?? '-'}',
                    style:
                        const TextStyle(
                      fontSize: 20,
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                ),
                Text(
                  status,
                  style: TextStyle(
                    fontWeight:
                        FontWeight.bold,
                    color:
                        statusColor(
                      status,
                    ),
                  ),
                ),
              ],
            ),

            const SizedBox(
              height: 8,
            ),

            Text(
              'Score '
              '${score.toStringAsFixed(1)}',
            ),

            const SizedBox(
              height: 4,
            ),

            Text(
              '${market['interval'] ?? '-'}'
              ' • Win '
              '${winRate.toStringAsFixed(1)}%'
              ' • PF '
              '${profitFactor.toStringAsFixed(2)}',
            ),

            const SizedBox(
              height: 4,
            ),

            Text(
              'Return '
              '${returnPct.toStringAsFixed(2)}%'
              ' • Trades '
              '${market['trades'] ?? '-'}',
            ),

            const SizedBox(
              height: 4,
            ),

            Text(
              'Threshold '
              '${market['threshold_pct'] ?? '-'}%'
              ' • Hold '
              '$holdText',
            ),
          ],
        ),
      ),
    );
  }

  // =========================================================
  // HOLDING TIME HELPERS
  // =========================================================

  int? _holdingMinutes(
    String interval,
    int holdingCandles,
  ) {
    final match = RegExp(
      r'^(\d+)(m|h)$',
    ).firstMatch(
      interval,
    );

    if (match == null) {
      return null;
    }

    final amount =
        int.tryParse(
      match.group(1)!,
    );

    if (amount == null) {
      return null;
    }

    final unit =
        match.group(2);

    final minutesPerCandle =
        unit == 'h'
            ? amount * 60
            : amount;

    return minutesPerCandle *
        holdingCandles;
  }

  String _formatMinutes(
    int minutes,
  ) {
    if (minutes < 60) {
      return '$minutes min';
    }

    if (minutes % 60 == 0) {
      final hours =
          minutes ~/ 60;

      return '$hours h';
    }

    final hours =
        minutes ~/ 60;

    final remainder =
        minutes % 60;

    return '$hours h '
        '$remainder min';
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
        formatRsi(
      sig?['rsi'],
    );

    final curve =
        (bt?['equity_curve']
                    as List?) ??
            const [];

    final topMarkets =
        (rankedMarkets?[
                    'top_markets']
                as List?) ??
            const [];

    final dynamic bestMarket =
        rankedMarkets?[
            'best_market'];

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'Jasong AI Trader V4.1',
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
            const Text(
              'AI-assisted paper trading',
              style: TextStyle(
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

            // =================================================
            // AI SIGNAL CARD
            // =================================================

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

            // =================================================
            // SIGNAL METRICS
            // =================================================

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

            // =================================================
            // BUTTONS
            // =================================================

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
                'Scan All Markets',
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
            // NORMAL LOADING
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

            // =================================================
            // MARKET SCAN PROGRESS
            // =================================================

            if (scanningMarkets)
              Padding(
                padding:
                    const EdgeInsets.all(
                  12,
                ),
                child: Column(
                  children: [
                    LinearProgressIndicator(
                      value:
                          markets.isEmpty
                              ? null
                              : scanProgress /
                                  markets.length,
                    ),

                    const SizedBox(
                      height: 8,
                    ),

                    Text(
                      'Scanning market '
                      '$scanProgress of '
                      '${markets.length}',
                    ),

                    if (
                      currentScanMarket !=
                      null
                    )
                      Padding(
                        padding:
                            const EdgeInsets.only(
                          top: 4,
                        ),
                        child:
                            Text(
                          currentScanMarket!,
                          style:
                              const TextStyle(
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),
                      ),

                    const SizedBox(
                      height: 6,
                    ),

                    Text(
                      '${marketResults.length} '
                      'completed • '
                      '${marketFailures.length} '
                      'skipped',
                      style:
                          const TextStyle(
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),

            // =================================================
            // ERROR
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
            // FAILED MARKETS
            // =================================================

            if (
              marketFailures.isNotEmpty
            ) ...[
              const SizedBox(
                height: 12,
              ),

              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    14,
                  ),
                  child: Column(
                    crossAxisAlignment:
                        CrossAxisAlignment.start,
                    children: [
                      Text(
                        '${marketFailures.length} '
                        'markets skipped',
                        style:
                            const TextStyle(
                          fontWeight:
                              FontWeight.bold,
                          color:
                              Colors.amberAccent,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      for (
                        final failure
                        in marketFailures
                      )
                        Padding(
                          padding:
                              const EdgeInsets.only(
                            bottom: 4,
                          ),
                          child: Text(
                            '${failure['market']}'
                            ' • '
                            '${failure['status']}',
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ],

            // =================================================
            // TOP MARKETS
            // =================================================

            if (
              rankedMarkets != null
            ) ...[
              const SizedBox(
                height: 18,
              ),

              const Text(
                'Top Markets',
                style:
                    TextStyle(
                  fontSize: 22,
                  fontWeight:
                      FontWeight.bold,
                ),
              ),

              const SizedBox(
                height: 8,
              ),

              if (
                topMarkets.isEmpty
              )
                const Card(
                  child: Padding(
                    padding:
                        EdgeInsets.all(
                      16,
                    ),
                    child: Text(
                      'No market currently '
                      'meets the V4 '
                      'qualification rules.',
                    ),
                  ),
                ),

              for (
                int i = 0;
                i <
                    topMarkets.length;
                i++
              )
                marketCard(
                  Map<
                      String,
                      dynamic
                  >.from(
                    topMarkets[i],
                  ),
                  i + 1,
                ),

              // ===============================================
              // BEST OPPORTUNITY
              // ===============================================

              if (
                bestMarket != null
              )
                Card(
                  child: Padding(
                    padding:
                        const EdgeInsets.all(
                      16,
                    ),
                    child: Column(
                      children: [
                        const Text(
                          'BEST OPPORTUNITY',
                          style:
                              TextStyle(
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),

                        const SizedBox(
                          height: 8,
                        ),

                        Text(
                          bestMarket[
                                  'market']
                              .toString(),
                          style:
                              const TextStyle(
                            fontSize: 30,
                            fontWeight:
                                FontWeight.w900,
                          ),
                        ),

                        const SizedBox(
                          height: 4,
                        ),

                        Text(
                          '${bestMarket['interval'] ?? '-'}'
                          ' • Score '
                          '${bestMarket['market_score'] ?? '-'}',
                        ),

                        const SizedBox(
                          height: 4,
                        ),

                        Text(
                          'Historical win rate '
                          '${formatPercent(bestMarket['win_rate'])}%',
                        ),

                        const SizedBox(
                          height: 4,
                        ),

                        Text(
                          'Profit factor '
                          '${bestMarket['profit_factor'] ?? '-'}',
                        ),

                        const SizedBox(
                          height: 4,
                        ),

                        Text(
                          'Threshold '
                          '${bestMarket['threshold_pct'] ?? '-'}%',
                        ),

                        const SizedBox(
                          height: 4,
                        ),

                        Text(
                          'Status '
                          '${bestMarket['status'] ?? '-'}',
                          style:
                              TextStyle(
                            color:
                                statusColor(
                              bestMarket[
                                          'status']
                                      ?.toString() ??
                                  '',
                            ),
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
            ],

            // =================================================
            // BACKTEST
            // =================================================

            if (bt != null) ...[
              const SizedBox(
                height: 18,
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
                  'no forced daily-profit '
                  'target, no live broker '
                  'execution, and no broker '
                  'password stored in the '
                  'app. Historical/model '
                  'results and market '
                  'rankings do not guarantee '
                  'future profit.',
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
