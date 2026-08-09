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
  Map<String, dynamic>? verifiedTrade;
  Map<String, dynamic>? verifiedResult;

  List<Map<String, dynamic>> validationHistory = [];

  bool busy = false;
  bool scanningMarkets = false;
  bool findingVerifiedTrade = false;

  String? currentValidationMarket;
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
  // HTTP POST JSON
  // =========================================================

  Future<Map<String, dynamic>> postJson(
    Uri uri,
    dynamic body, {
    int timeoutSeconds = 120,
  }) async {
    final response = await http
        .post(
          uri,
          headers: {
            'Content-Type':
                'application/json',
          },
          body: jsonEncode(
            body,
          ),
        )
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
          'symbol':
              symbol.text.trim(),
          'risk_mode':
              risk,
          'balance':
              balance.text.trim(),
        },
      );

      final result = await getJson(
        uri,
      );

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
          'symbol':
              symbol.text.trim(),
          'risk_mode':
              risk,
          'starting_balance':
              balance.text.trim(),
        },
      );

      final result = await getJson(
        uri,
      );

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
  // FAST MARKET SCANNER
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
        timeoutSeconds: 120,
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
  // DEEP VALIDATE ONE CANDIDATE
  // =========================================================

  Future<Map<String, dynamic>>
      deepValidateOne(
    Map<String, dynamic> candidate,
  ) async {
    final uri = Uri.parse(
      '$apiBase/deep-validate',
    ).replace(
      queryParameters: {
        'risk_mode':
            risk,
        'starting_balance':
            balance.text.trim(),
        'payout':
            '0.80',
        'max_candidates':
            '1',
      },
    );

    final body = [
      {
        'market':
            candidate['market'],
        'symbol':
            candidate['symbol'],
        'fast_score':
            candidate['fast_score'],
        'direction':
            candidate['direction'],
        'status':
            candidate['status'],
      }
    ];

    return postJson(
      uri,
      body,
      timeoutSeconds: 120,
    );
  }

  // =========================================================
  // V4.5 FIND VERIFIED TRADE
  // =========================================================

  Future<void> findVerifiedTrade() async {
    if (busy) return;

    setState(() {
      busy = true;

      findingVerifiedTrade = true;

      scanningMarkets = true;

      error = null;

      verifiedTrade = null;

      verifiedResult = null;

      validationHistory = [];

      currentValidationMarket =
          'Scanning all markets...';
    });

    try {
      // -----------------------------------------------------
      // STEP 1: FAST SCAN
      // -----------------------------------------------------

      final scanUri = Uri.parse(
        '$apiBase/fast-scan',
      ).replace(
        queryParameters: {
          'period':
              '5d',
          'interval':
              '15m',
          'top_n':
              '3',
        },
      );

      final scanResult = await getJson(
        scanUri,
        timeoutSeconds: 120,
      );

      if (!mounted) return;

      final candidatesRaw =
          (scanResult[
                      'top_candidates']
                  as List?) ??
              const [];

      final candidates = [
        for (final item
            in candidatesRaw)
          if (item is Map)
            Map<String, dynamic>.from(
              item,
            ),
      ];

      setState(() {
        fastScan = scanResult;

        scanningMarkets = false;
      });

      if (candidates.isEmpty) {
        throw Exception(
          'Fast scanner found no '
          'candidates to validate.',
        );
      }

      // -----------------------------------------------------
      // STEP 2: DEEP VALIDATE TOP 3 SEQUENTIALLY
      // -----------------------------------------------------

      for (
        int i = 0;
        i < candidates.length && i < 3;
        i++
      ) {
        if (!mounted) return;

        final candidate =
            candidates[i];

        final market =
            candidate['market']
                    ?.toString() ??
                'Unknown';

        setState(() {
          currentValidationMarket =
              'Deep validating #${i + 1} '
              '$market...';
        });

        Map<String, dynamic>
            deepResult;

        try {
          deepResult =
              await deepValidateOne(
            candidate,
          );
        } catch (e) {
          if (!mounted) return;

          final failed = {
            'position':
                i + 1,
            'market':
                market,
            'status':
                'ERROR',
            'verified':
                false,
            'message':
                e.toString(),
          };

          setState(() {
            validationHistory = [
              ...validationHistory,
              failed,
            ];
          });

          continue;
        }

        final finalMarketRaw =
            deepResult[
                'final_market'];

        Map<String, dynamic>?
            finalMarket;

        if (finalMarketRaw
            is Map) {
          finalMarket =
              Map<String, dynamic>.from(
            finalMarketRaw,
          );
        }

        final isVerified =
            finalMarket?[
                    'verified'] ==
                true;

        final deepStatus =
            finalMarket?[
                        'status']
                    ?.toString() ??
                deepResult[
                        'final_status']
                    ?.toString() ??
                'UNKNOWN';

        final historyItem = {
          'position':
              i + 1,

          'market':
              market,

          'fast_score':
              candidate[
                  'fast_score'],

          'fast_direction':
              candidate[
                  'direction'],

          'deep_status':
              deepStatus,

          'verified':
              isVerified,

          'deep_score':
              finalMarket?[
                  'deep_score'],

          'win_rate':
              finalMarket?[
                  'win_rate'],

          'profit_factor':
              finalMarket?[
                  'profit_factor'],

          'max_drawdown':
              finalMarket?[
                  'max_drawdown'],

          'trades':
              finalMarket?[
                  'trades'],

          'interval':
              finalMarket?[
                  'interval'],

          'period':
              finalMarket?[
                  'period'],

          'holding_candles':
              finalMarket?[
                  'holding_candles'],
        };

        if (!mounted) return;

        setState(() {
          validationHistory = [
            ...validationHistory,
            historyItem,
          ];
        });

        if (!isVerified ||
            finalMarket == null) {
          continue;
        }

        final fastDirection =
            candidate[
                    'direction']
                ?.toString();

        final deepDirection =
            finalMarket[
                    'direction']
                ?.toString();

        // ---------------------------------------------------
        // Require direction agreement
        // ---------------------------------------------------

        if (fastDirection !=
            deepDirection) {
          setState(() {
            validationHistory[
                    validationHistory
                            .length -
                        1]
                ['deep_status'] =
                'DIRECTION_MISMATCH';

            validationHistory[
                    validationHistory
                            .length -
                        1]
                ['verified'] =
                false;
          });

          continue;
        }

        // ---------------------------------------------------
        // VERIFIED TRADE FOUND
        // ---------------------------------------------------

        final verified = {
          ...finalMarket,
          'fast_rank':
              i + 1,
          'fast_score':
              candidate[
                  'fast_score'],
          'fast_direction':
              candidate[
                  'direction'],
          'direction_agreement':
              true,
        };

        setState(() {
          verifiedTrade =
              verified;

          verifiedResult =
              deepResult;

          currentValidationMarket =
              'Verified trade found';
        });

        break;
      }

      // -----------------------------------------------------
      // NO VERIFIED TRADE
      // -----------------------------------------------------

      if (verifiedTrade == null &&
          mounted) {
        setState(() {
          currentValidationMarket =
              'No verified trade found';
        });
      }
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error =
            'Verified trade search '
            'failed: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;

          findingVerifiedTrade =
              false;

          scanningMarkets =
              false;
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
        market['symbol']
            ?.toString();

    if (marketSymbol == null ||
        marketSymbol.isEmpty) {
      return;
    }

    setState(() {
      symbol.text =
          marketSymbol;

      error = null;
    });

    await refreshSignal();
  }

  // =========================================================
  // ANALYSE VERIFIED TRADE
  // =========================================================

  Future<void>
      analyseVerifiedTrade() async {
    if (verifiedTrade == null) {
      return;
    }

    await analyseMarket(
      verifiedTrade!,
    );
  }

  // =========================================================
  // RECORD PAPER TRADE
  // =========================================================

  Future<void>
      recordPaperTrade() async {
    if (busy) return;

    final decision =
        sig?['decision']
            ?.toString();

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
          'symbol':
              symbol.text.trim(),

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
          .post(
            uri,
          )
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
      case 'VERIFIED':
        return Colors.greenAccent;

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

      case 'DIRECTION_MISMATCH':
        return Colors.orangeAccent;

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
              const EdgeInsets.all(
            14,
          ),
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
  // FAST MARKET CARD
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

    final reasons =
        (market['reasons']
                    as List?) ??
            const [];

    return Card(
      child: InkWell(
        borderRadius:
            BorderRadius.circular(
          12,
        ),
        onTap: busy
            ? null
            : () {
                analyseMarket(
                  market,
                );
              },
        child: Padding(
          padding:
              const EdgeInsets.all(
            14,
          ),
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
                '${formatPrice(market['price'])}'
                ' • RSI '
                '${formatNumber(market['rsi'])}',
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
            ],
          ),
        ),
      ),
    );
  }

  // =========================================================
  // VALIDATION HISTORY CARD
  // =========================================================

  Widget validationCard(
    Map<String, dynamic> item,
  ) {
    final status =
        item['deep_status']
                ?.toString() ??
            item['status']
                ?.toString() ??
            'UNKNOWN';

    final verified =
        item['verified'] == true;

    return Card(
      child: Padding(
        padding:
            const EdgeInsets.all(
          14,
        ),
        child: Column(
          crossAxisAlignment:
              CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '#${item['position']} '
                    '${item['market']}',
                    style:
                        const TextStyle(
                      fontSize: 18,
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                ),

                Icon(
                  verified
                      ? Icons.verified
                      : Icons.cancel_outlined,
                  color: verified
                      ? Colors.greenAccent
                      : statusColor(
                          status,
                        ),
                ),
              ],
            ),

            const SizedBox(
              height: 6,
            ),

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

            if (item['deep_score']
                != null)
              Text(
                'Deep score: '
                '${formatNumber(item['deep_score'])}',
              ),

            if (item['win_rate']
                != null)
              Text(
                'Win rate: '
                '${formatPercent(item['win_rate'])}%',
              ),

            if (item['profit_factor']
                != null)
              Text(
                'Profit factor: '
                '${formatNumber(item['profit_factor'])}',
              ),

            if (item['trades']
                != null)
              Text(
                'Trades: '
                '${item['trades']}',
              ),
          ],
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
          'Jasong AI Trader V4.5',
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
              const EdgeInsets.all(
            14,
          ),

          children: [
            // =================================================
            // LIVE SIGNAL
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
                      value:
                          item,
                      child:
                          Text(
                        item,
                      ),
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

            FilledButton.icon(
              onPressed:
                  busy
                      ? null
                      : findVerifiedTrade,

              icon:
                  const Icon(
                Icons.verified,
              ),

              label:
                  const Text(
                'Find Verified Trade',
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
              !scanningMarkets &&
              !findingVerifiedTrade
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
            // FAST SCAN LOADING
            // =================================================

            if (
              scanningMarkets &&
              !findingVerifiedTrade
            )
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
            // V4.5 VERIFIED SEARCH LOADING
            // =================================================

            if (findingVerifiedTrade)
              Padding(
                padding:
                    const EdgeInsets.all(
                  16,
                ),
                child: Column(
                  children: [
                    const LinearProgressIndicator(),

                    const SizedBox(
                      height: 10,
                    ),

                    const Text(
                      'Finding verified trade...',
                      style:
                          TextStyle(
                        fontWeight:
                            FontWeight.bold,
                      ),
                    ),

                    const SizedBox(
                      height: 6,
                    ),

                    Text(
                      currentValidationMarket ??
                          'Starting...',
                      textAlign:
                          TextAlign.center,
                    ),

                    const SizedBox(
                      height: 6,
                    ),

                    Text(
                      '${validationHistory.length} '
                      'deep validations completed',
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
            // VERIFIED TRADE RESULT
            // =================================================

            if (verifiedTrade != null) ...[
              const SizedBox(
                height: 20,
              ),

              const Divider(),

              const SizedBox(
                height: 10,
              ),

              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    18,
                  ),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.verified,
                        size: 44,
                        color:
                            Colors.greenAccent,
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'VERIFIED OPPORTUNITY',
                        style:
                            TextStyle(
                          fontSize: 16,
                          fontWeight:
                              FontWeight.bold,
                          color:
                              Colors.greenAccent,
                        ),
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      Text(
                        '${verifiedTrade!['market']}',
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
                        '${verifiedTrade!['direction']}',
                        style:
                            TextStyle(
                          fontSize: 30,
                          fontWeight:
                              FontWeight.w900,
                          color:
                              decisionColor(
                            verifiedTrade![
                                    'direction']
                                .toString(),
                          ),
                        ),
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      Row(
                        children: [
                          metric(
                            'Deep score',
                            formatNumber(
                              verifiedTrade![
                                  'deep_score'],
                            ),
                          ),

                          metric(
                            'Win rate',
                            '${formatPercent(
                              verifiedTrade![
                                  'win_rate'],
                            )}%',
                          ),
                        ],
                      ),

                      Row(
                        children: [
                          metric(
                            'Profit factor',
                            formatNumber(
                              verifiedTrade![
                                  'profit_factor'],
                            ),
                          ),

                          metric(
                            'Max DD',
                            '${formatPercent(
                              verifiedTrade![
                                  'max_drawdown'],
                            )}%',
                          ),
                        ],
                      ),

                      Row(
                        children: [
                          metric(
                            'Trades',
                            '${verifiedTrade!['trades'] ?? '-'}',
                          ),

                          metric(
                            'Fast rank',
                            '#${verifiedTrade!['fast_rank'] ?? '-'}',
                          ),
                        ],
                      ),

                      const SizedBox(
                        height: 10,
                      ),

                      Text(
                        'Validated setup: '
                        '${verifiedTrade!['interval'] ?? '-'}'
                        ' • '
                        '${verifiedTrade!['period'] ?? '-'}'
                        ' • Hold '
                        '${verifiedTrade!['holding_candles'] ?? '-'} candles',
                        textAlign:
                            TextAlign.center,
                      ),

                      const SizedBox(
                        height: 4,
                      ),

                      Text(
                        'Threshold: '
                        '${verifiedTrade!['threshold_pct'] ?? '-'}%',
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      FilledButton.icon(
                        onPressed:
                            busy
                                ? null
                                : analyseVerifiedTrade,

                        icon:
                            const Icon(
                          Icons.psychology,
                        ),

                        label:
                            const Text(
                          'Check Current Live Signal',
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'Verified means the historical '
                        'validation rules passed. '
                        'It does not guarantee the next '
                        'trade will win.',
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

            // =================================================
            // NO VERIFIED TRADE
            // =================================================

            if (
              !findingVerifiedTrade &&
              verifiedTrade == null &&
              validationHistory.isNotEmpty
            ) ...[
              const SizedBox(
                height: 20,
              ),

              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    16,
                  ),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.hourglass_empty,
                        size: 36,
                        color:
                            Colors.amberAccent,
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'NO VERIFIED TRADE',
                        style:
                            TextStyle(
                          fontSize: 20,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      const SizedBox(
                        height: 6,
                      ),

                      Text(
                        '${validationHistory.length} '
                        'top markets were tested.',
                      ),

                      const SizedBox(
                        height: 4,
                      ),

                      const Text(
                        'No trade should be forced when '
                        'the validation criteria are not met.',
                        textAlign:
                            TextAlign.center,
                      ),
                    ],
                  ),
                ),
              ),
            ],

            // =================================================
            // VALIDATION HISTORY
            // =================================================

            if (validationHistory.isNotEmpty) ...[
              const SizedBox(
                height: 16,
              ),

              const Text(
                'Deep Validation History',
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

              for (
                final item
                in validationHistory
              )
                validationCard(
                  item,
                ),
            ],

            // =================================================
            // FAST SCAN RESULTS
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
                    'Fast Market Scanner',
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

              for (
                int i = 0;
                i < topCandidates.length;
                i++
              )
                fastMarketCard(
                  Map<String, dynamic>.from(
                    topCandidates[i],
                  ),
                  i + 1,
                ),

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
                      i < ranking.length;
                      i++
                    )
                      ListTile(
                        leading:
                            CircleAvatar(
                          child:
                              Text(
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
                                      Map<String, dynamic>.from(
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
            // BACKTEST
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
                    '${formatPercent(
                      bt!['win_rate'],
                    )}%',
                  ),
                ],
              ),

              Row(
                children: [
                  metric(
                    'Return',
                    '${formatPercent(
                      bt!['return_pct'],
                    )}%',
                  ),

                  metric(
                    'Max DD',
                    '${formatPercent(
                      bt!['max_drawdown'],
                    )}%',
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
                        show:
                            false,
                      ),

                      borderData:
                          FlBorderData(
                        show:
                            true,
                      ),

                      lineBarsData: [
                        LineChartBarData(
                          isCurved:
                              true,

                          dotData:
                              const FlDotData(
                            show:
                                false,
                          ),

                          spots: [
                            for (
                              int i = 0;
                              i < curve.length;
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
                  'Fast scores, deep-validation results, '
                  'historical win rates and model outputs '
                  'do not guarantee future profit.',
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
