import 'dart:async';
import 'dart:convert';

import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

void main() {
  runApp(const JasongApp());
}

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
  State<HomePage> createState() {
    return _HomePageState();
  }
}

class _HomePageState extends State<HomePage> {
  // =========================================================
  // SETTINGS
  // =========================================================

  final TextEditingController symbol =
      TextEditingController(
    text: 'EURUSD=X',
  );

  final TextEditingController balance =
      TextEditingController(
    text: '10000',
  );

  String risk = 'Balanced';

  final String apiBase =
      const String.fromEnvironment(
    'API_BASE_URL',
    defaultValue:
        'https://jasong-ai-trader-v2.onrender.com',
  );

  // =========================================================
  // DATA
  // =========================================================

  Map<String, dynamic>? sig;
  Map<String, dynamic>? bt;

  Map<String, dynamic>? fastScan;

  Map<String, dynamic>? verifiedTrade;

  List<Map<String, dynamic>>
      validationHistory = [];

  // =========================================================
  // STATE
  // =========================================================

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
    int timeoutSeconds = 120,
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
        'HTTP ${response.statusCode}: '
        '${response.body}',
      );
    }

    final decoded = jsonDecode(
      response.body,
    );

    if (decoded is! Map) {
      throw Exception(
        'Invalid JSON response from server',
      );
    }

    return Map<String, dynamic>.from(
      decoded,
    );
  }

  // =========================================================
  // POST JSON WITH RETRY
  // =========================================================

  Future<Map<String, dynamic>> postJson(
    Uri uri,
    dynamic body, {
    int timeoutSeconds = 240,
    int retries = 1,
  }) async {
    Exception? lastError;

    for (
      int attempt = 0;
      attempt <= retries;
      attempt++
    ) {
      try {
        final response = await http
            .post(
              uri,
              headers: {
                'Accept':
                    'application/json',
                'Content-Type':
                    'application/json',
              },
              body: jsonEncode(
                body,
              ),
            )
            .timeout(
              Duration(
                seconds:
                    timeoutSeconds,
              ),
            );

        if (response.statusCode == 200) {
          final decoded = jsonDecode(
            response.body,
          );

          if (decoded is! Map) {
            throw Exception(
              'Backend returned an '
              'unexpected response format',
            );
          }

          return Map<String, dynamic>.from(
            decoded,
          );
        }

        final temporaryError =
            response.statusCode == 502 ||
                response.statusCode == 503 ||
                response.statusCode == 504;

        lastError = Exception(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );

        if (!temporaryError) {
          throw lastError;
        }
      } on TimeoutException catch (e) {
        lastError = Exception(
          'Deep validation timed out: $e',
        );
      } on FormatException catch (e) {
        lastError = Exception(
          'Invalid JSON from backend: $e',
        );
      } catch (e) {
        lastError = Exception(
          e.toString(),
        );
      }

      if (attempt < retries) {
        await Future.delayed(
          const Duration(
            seconds: 5,
          ),
        );
      }
    }

    throw lastError ??
        Exception(
          'Unknown backend error',
        );
  }

  // =========================================================
  // REFRESH LIVE SIGNAL
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
        timeoutSeconds: 180,
      );

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
  // FAST SCAN
  // =========================================================

  Future<Map<String, dynamic>>
      runFastScanRequest() async {
    final uri = Uri.parse(
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

    return getJson(
      uri,
      timeoutSeconds: 180,
    );
  }

  Future<void> scanAllMarkets() async {
    if (busy) {
      return;
    }

    setState(() {
      busy = true;
      scanningMarkets = true;
      error = null;
    });

    try {
      final result =
          await runFastScanRequest();

      if (!mounted) {
        return;
      }

      setState(() {
        fastScan = result;
      });
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Fast scan failed: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          scanningMarkets = false;
          busy = false;
        });
      }
    }
  }

  // =========================================================
  // DEEP VALIDATE ONE MARKET
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
            '0.8',
        'max_candidates':
            '1',
      },
    );

    final requestBody = [
      {
        'market':
            candidate['market'],
        'symbol':
            candidate['symbol'],
        'fast_score':
            candidate['fast_score'] ??
                candidate['score'] ??
                0.0,
        'direction':
            candidate['direction'] ??
                'WAIT',
        'status':
            candidate['status'] ??
                'UNKNOWN',
      }
    ];

    return postJson(
      uri,
      requestBody,
      timeoutSeconds: 240,
      retries: 1,
    );
  }

  // =========================================================
  // FIND VERIFIED TRADE
  // =========================================================

  Future<void> findVerifiedTrade() async {
    if (busy) {
      return;
    }

    setState(() {
      busy = true;

      scanningMarkets = true;

      findingVerifiedTrade = true;

      verifiedTrade = null;

      validationHistory = [];

      error = null;

      currentValidationMarket =
          'Scanning 9 markets...';
    });

    try {
      // =====================================================
      // STEP 1: FAST SCAN
      // =====================================================

      final scanResult =
          await runFastScanRequest();

      if (!mounted) {
        return;
      }

      setState(() {
        fastScan = scanResult;
        scanningMarkets = false;
      });

      final rawCandidates =
          scanResult[
                  'top_candidates']
              as List?;

      if (rawCandidates == null ||
          rawCandidates.isEmpty) {
        throw Exception(
          'Fast scan returned no '
          'candidates',
        );
      }

      final candidates = <
          Map<String, dynamic>>[];

      for (final item
          in rawCandidates) {
        if (item is Map) {
          candidates.add(
            Map<String, dynamic>.from(
              item,
            ),
          );
        }
      }

      if (candidates.isEmpty) {
        throw Exception(
          'No valid candidate records '
          'were returned',
        );
      }

      // =====================================================
      // STEP 2: VALIDATE TOP THREE
      // =====================================================

      final maximum =
          candidates.length > 3
              ? 3
              : candidates.length;

      for (
        int index = 0;
        index < maximum;
        index++
      ) {
        final candidate =
            candidates[index];

        final market =
            candidate['market']
                    ?.toString() ??
                'UNKNOWN';

        if (!mounted) {
          return;
        }

        setState(() {
          currentValidationMarket =
              'Deep validating '
              '#${index + 1} '
              '$market...';
        });

        try {
          final deep =
              await deepValidateOne(
            candidate,
          );

          // ===============================================
          // READ EXACT V4.6 RESPONSE
          // ===============================================

          final finalStatus =
              deep['final_status']
                      ?.toString() ??
                  'UNKNOWN';

          Map<String, dynamic>?
              finalMarket;

          final rawFinalMarket =
              deep['final_market'];

          if (rawFinalMarket is Map) {
            finalMarket =
                Map<String, dynamic>.from(
              rawFinalMarket,
            );
          }

          // ===============================================
          // VERIFIED?
          // ===============================================

          final verifiedByMarket =
              finalMarket?[
                      'verified'] ==
                  true;

          final verifiedByStatus =
              finalStatus ==
                  'VERIFIED_TRADE';

          final verified =
              verifiedByMarket ||
                  verifiedByStatus;

          final deepStatus =
              finalMarket?[
                          'status']
                      ?.toString() ??
                  finalStatus;

          final historyItem =
              <String, dynamic>{
            'position':
                index + 1,

            'market':
                market,

            'symbol':
                candidate[
                    'symbol'],

            'fast_score':
                candidate[
                    'fast_score'],

            'fast_direction':
                candidate[
                    'direction'],

            'deep_status':
                deepStatus,

            'verified':
                verified,

            'deep_score':
                finalMarket?[
                    'deep_score'],

            'trades':
                finalMarket?[
                    'trades'],

            'wins':
                finalMarket?[
                    'wins'],

            'losses':
                finalMarket?[
                    'losses'],

            'win_rate':
                finalMarket?[
                    'win_rate'],

            'profit_factor':
                finalMarket?[
                    'profit_factor'],

            'return_pct':
                finalMarket?[
                    'return_pct'],

            'max_drawdown':
                finalMarket?[
                    'max_drawdown'],

            'interval':
                finalMarket?[
                    'interval'],

            'period':
                finalMarket?[
                    'period'],

            'threshold_pct':
                finalMarket?[
                    'threshold_pct'],

            'holding_candles':
                finalMarket?[
                    'holding_candles'],
          };

          if (!mounted) {
            return;
          }

          setState(() {
            validationHistory.add(
              historyItem,
            );
          });

          // ===============================================
          // NOT VERIFIED → NEXT MARKET
          // ===============================================

          if (!verified ||
              finalMarket == null) {
            continue;
          }

          // ===============================================
          // DIRECTION AGREEMENT
          // ===============================================

          final fastDirection =
              candidate[
                      'direction']
                  ?.toString();

          final deepDirection =
              finalMarket[
                      'direction']
                  ?.toString();

          if (fastDirection !=
              deepDirection) {
            setState(() {
              validationHistory.last[
                      'verified'] =
                  false;

              validationHistory.last[
                      'deep_status'] =
                  'DIRECTION_MISMATCH';
            });

            continue;
          }

          // ===============================================
          // VERIFIED TRADE FOUND
          // ===============================================

          final result = <
              String, dynamic>{
            ...finalMarket,

            'fast_rank':
                index + 1,

            'fast_score':
                candidate[
                    'fast_score'],

            'fast_direction':
                fastDirection,

            'direction_agreement':
                true,
          };

          if (!mounted) {
            return;
          }

          setState(() {
            verifiedTrade = result;

            currentValidationMarket =
                '$market VERIFIED';
          });

          // STOP LOOP
          break;
        } catch (e) {
          if (!mounted) {
            return;
          }

          setState(() {
            validationHistory.add({
              'position':
                  index + 1,

              'market':
                  market,

              'symbol':
                  candidate[
                      'symbol'],

              'fast_score':
                  candidate[
                      'fast_score'],

              'fast_direction':
                  candidate[
                      'direction'],

              'deep_status':
                  'ERROR',

              'verified':
                  false,

              'error':
                  e.toString(),
            });
          });
        }
      }

      // =====================================================
      // FINISHED WITHOUT VERIFIED TRADE
      // =====================================================

      if (mounted &&
          verifiedTrade == null) {
        final hasSuccessfulValidation =
            validationHistory.any(
          (item) =>
              item['deep_status'] !=
              'ERROR',
        );

        setState(() {
          if (hasSuccessfulValidation) {
            currentValidationMarket =
                'No verified trade found';
          } else {
            currentValidationMarket =
                'Validation failed';
          }
        });
      }
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Verified trade search '
            'failed: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;

          scanningMarkets = false;

          findingVerifiedTrade = false;
        });
      }
    }
  }

  // =========================================================
  // ANALYSE SELECTED MARKET
  // =========================================================

  Future<void> analyseMarket(
    Map<String, dynamic> market,
  ) async {
    final selectedSymbol =
        market['symbol']
            ?.toString();

    if (selectedSymbol == null ||
        selectedSymbol.isEmpty) {
      return;
    }

    setState(() {
      symbol.text = selectedSymbol;
      error = null;
    });

    await refreshSignal();
  }

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
  // PAPER TRADE
  // =========================================================

  Future<void>
      recordPaperTrade() async {
    if (busy || sig == null) {
      return;
    }

    final direction =
        sig!['decision']
            ?.toString();

    if (direction != 'BUY' &&
        direction != 'SELL') {
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
              direction!,

          'confidence':
              '${sig!['confidence']}',

          'entry_price':
              '${sig!['price']}',

          'stake':
              '${sig!['suggested_paper_stake']}',
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
  // INIT
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
  // UI HELPERS
  // =========================================================

  Color decisionColor(
    String value,
  ) {
    if (value == 'BUY') {
      return Colors.greenAccent;
    }

    if (value == 'SELL') {
      return Colors.redAccent;
    }

    return Colors.amberAccent;
  }

  Color statusColor(
    String value,
  ) {
    switch (value) {
      case 'VERIFIED':
        return Colors.greenAccent;

      case 'VERIFIED_TRADE':
        return Colors.greenAccent;

      case 'STRONG':
        return Colors.greenAccent;

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

  String formatPrice(
    dynamic value,
  ) {
    if (value is num) {
      if (value >= 100) {
        return value
            .toDouble()
            .toStringAsFixed(3);
      }

      return value
          .toDouble()
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
  // VALIDATION CARD
  // =========================================================

  Widget validationCard(
    Map<String, dynamic> item,
  ) {
    final status =
        item['deep_status']
                ?.toString() ??
            'UNKNOWN';

    final verified =
        item['verified'] == true;

    final errorMessage =
        item['error']
            ?.toString();

    return Card(
      child: Padding(
        padding:
            const EdgeInsets.all(
          16,
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
                      fontSize: 20,
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),
                ),

                Icon(
                  verified
                      ? Icons.verified
                      : Icons.cancel_outlined,
                  color:
                      verified
                          ? Colors.greenAccent
                          : statusColor(
                              status,
                            ),
                ),
              ],
            ),

            const SizedBox(
              height: 8,
            ),

            Text(
              status,
              style:
                  TextStyle(
                fontSize: 16,
                fontWeight:
                    FontWeight.bold,
                color:
                    statusColor(
                  status,
                ),
              ),
            ),

            if (item['deep_score'] !=
                null) ...[
              const SizedBox(
                height: 6,
              ),

              Text(
                'Deep score: '
                '${formatNumber(item['deep_score'])}',
              ),
            ],

            if (item['win_rate'] !=
                null)
              Text(
                'Win rate: '
                '${formatPercent(item['win_rate'])}%',
              ),

            if (item['trades'] != null)
              Text(
                'Trades: '
                '${item['trades']}',
              ),

            if (item['profit_factor'] !=
                null)
              Text(
                'Profit factor: '
                '${formatNumber(item['profit_factor'])}',
              ),

            if (errorMessage != null) ...[
              const SizedBox(
                height: 10,
              ),

              Text(
                errorMessage,
                style:
                    const TextStyle(
                  color:
                      Colors.redAccent,
                  fontSize: 12,
                ),
              ),
            ],
          ],
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
        (market['fast_score'] ??
                0.0)
            as num;

    final reasons =
        (market['reasons']
                    as List?) ??
            const [];

    return Card(
      child: InkWell(
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
            16,
          ),
          child: Column(
            crossAxisAlignment:
                CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      '#$rank '
                      '$marketName',
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
                height: 8,
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
                height: 8,
              ),

              Text(
                'Price '
                '${formatPrice(market['price'])}'
                ' • RSI '
                '${formatNumber(market['rsi'])}',
              ),

              Text(
                '${market['interval'] ?? '-'}'
                ' • '
                '${market['period'] ?? '-'}',
              ),

              if (reasons.isNotEmpty) ...[
                const SizedBox(
                  height: 8,
                ),

                Text(
                  reasons
                      .take(3)
                      .join(' • '),
                  style:
                      const TextStyle(
                    color:
                        Colors.white70,
                    fontSize: 12,
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
  // MAIN UI
  // =========================================================

  @override
  Widget build(
    BuildContext context,
  ) {
    final liveDecision =
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

    final curve =
        (bt?['equity_curve']
                    as List?) ??
            const [];

    final topCandidates =
        (fastScan?[
                    'top_candidates']
                as List?) ??
            const [];

    final allRanking =
        (fastScan?['ranking']
                    as List?) ??
            const [];

    final hasValidationErrors =
        validationHistory.isNotEmpty &&
            validationHistory.every(
              (item) =>
                  item[
                      'deep_status'] ==
                  'ERROR',
            );

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          'Jasong AI Trader V4.6',
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
                  TextInputType.number,
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
              value:
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
                    (value) =>
                        DropdownMenuItem<
                            String>(
                      value:
                          value,
                      child:
                          Text(
                        value,
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
                      liveDecision,
                      style:
                          TextStyle(
                        fontSize: 44,
                        fontWeight:
                            FontWeight.w900,
                        color:
                            decisionColor(
                          liveDecision,
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
                  formatPrice(
                    sig?['price'],
                  ),
                ),

                metric(
                  'RSI',
                  formatNumber(
                    sig?['rsi'],
                  ),
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
              height: 12,
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
                            liveDecision,
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

            if (findingVerifiedTrade)
              Padding(
                padding:
                    const EdgeInsets.all(
                  18,
                ),
                child: Column(
                  children: [
                    const LinearProgressIndicator(),

                    const SizedBox(
                      height: 10,
                    ),

                    Text(
                      currentValidationMarket ??
                          'Working...',
                      textAlign:
                          TextAlign.center,
                      style:
                          const TextStyle(
                        fontWeight:
                            FontWeight.bold,
                      ),
                    ),

                    const SizedBox(
                      height: 4,
                    ),

                    const Text(
                      'Please allow deep validation '
                      'to finish. This can take longer '
                      'than the fast scan.',
                      textAlign:
                          TextAlign.center,
                      style:
                          TextStyle(
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),

            if (busy &&
                !findingVerifiedTrade &&
                !scanningMarkets)
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

            if (error != null)
              Padding(
                padding:
                    const EdgeInsets.only(
                  top: 12,
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
            // VERIFIED OPPORTUNITY
            // =================================================

            if (verifiedTrade != null) ...[
              const SizedBox(
                height: 22,
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
                        color:
                            Colors.greenAccent,
                        size: 48,
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'VERIFIED OPPORTUNITY',
                        style:
                            TextStyle(
                          color:
                              Colors.greenAccent,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      Text(
                        '${verifiedTrade!['market']}',
                        style:
                            const TextStyle(
                          fontSize: 36,
                          fontWeight:
                              FontWeight.w900,
                        ),
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
                            '${verifiedTrade!['trades']}',
                          ),

                          metric(
                            'Fast rank',
                            '#${verifiedTrade!['fast_rank']}',
                          ),
                        ],
                      ),

                      const SizedBox(
                        height: 10,
                      ),

                      Text(
                        '${verifiedTrade!['interval']}'
                        ' • '
                        '${verifiedTrade!['period']}'
                        ' • Hold '
                        '${verifiedTrade!['holding_candles']} candles',
                        textAlign:
                            TextAlign.center,
                      ),

                      Text(
                        'Threshold '
                        '${verifiedTrade!['threshold_pct']}%',
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
                        height: 10,
                      ),

                      const Text(
                        'VERIFIED means this historical '
                        'setup passed the validation rules. '
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
            // VALIDATION ERROR
            // =================================================

            if (!findingVerifiedTrade &&
                verifiedTrade == null &&
                hasValidationErrors)
              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    18,
                  ),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.error_outline,
                        size: 42,
                        color:
                            Colors.redAccent,
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'VALIDATION ERROR',
                        style:
                            TextStyle(
                          fontSize: 22,
                          fontWeight:
                              FontWeight.bold,
                          color:
                              Colors.redAccent,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'The markets were not rejected. '
                        'Deep validation failed to complete. '
                        'See the error details below.',
                        textAlign:
                            TextAlign.center,
                      ),
                    ],
                  ),
                ),
              ),

            // =================================================
            // NO VERIFIED TRADE
            // =================================================

            if (!findingVerifiedTrade &&
                verifiedTrade == null &&
                validationHistory.isNotEmpty &&
                !hasValidationErrors)
              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    18,
                  ),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.hourglass_empty,
                        size: 42,
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
                          fontSize: 22,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      Text(
                        '${validationHistory.length} '
                        'candidate(s) were evaluated.',
                      ),

                      const Text(
                        'No trade should be forced when '
                        'the validation rules are not met.',
                        textAlign:
                            TextAlign.center,
                      ),
                    ],
                  ),
                ),
              ),

            // =================================================
            // DEEP VALIDATION HISTORY
            // =================================================

            if (validationHistory.isNotEmpty) ...[
              const SizedBox(
                height: 20,
              ),

              const Text(
                'Deep Validation History',
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

              for (final item
                  in validationHistory)
                validationCard(
                  item,
                ),
            ],

            // =================================================
            // FAST SCAN
            // =================================================

            if (fastScan != null) ...[
              const SizedBox(
                height: 24,
              ),

              const Divider(),

              const SizedBox(
                height: 12,
              ),

              const Text(
                '⚡ Fast Market Scanner',
                style:
                    TextStyle(
                  fontSize: 23,
                  fontWeight:
                      FontWeight.bold,
                ),
              ),

              const SizedBox(
                height: 12,
              ),

              Row(
                children: [
                  metric(
                    'Scanned',
                    '${fastScan!['markets_tested'] ?? 0}',
                  ),

                  metric(
                    'Successful',
                    '${fastScan!['markets_successful'] ?? 0}',
                  ),
                ],
              ),

              Row(
                children: [
                  metric(
                    'Candidates',
                    '${fastScan!['candidates_found'] ?? 0}',
                  ),

                  metric(
                    'Failures',
                    '${fastScan!['markets_failed'] ?? 0}',
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
                  fontSize: 21,
                  fontWeight:
                      FontWeight.bold,
                ),
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

              if (allRanking.isNotEmpty)
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
                      i < allRanking.length;
                      i++
                    )
                      ListTile(
                        leading:
                            Text(
                          '#${i + 1}',
                        ),
                        title:
                            Text(
                          '${allRanking[i]['market']}',
                        ),
                        subtitle:
                            Text(
                          '${allRanking[i]['direction']}'
                          ' • '
                          '${allRanking[i]['status']}',
                        ),
                        trailing:
                            Text(
                          '${allRanking[i]['fast_score']}',
                        ),
                      ),
                  ],
                ),
            ],

            // =================================================
            // BACKTEST
            // =================================================

            if (bt != null) ...[
              const SizedBox(
                height: 24,
              ),

              const Text(
                'Backtest',
                style:
                    TextStyle(
                  fontSize: 21,
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
              height: 20,
            ),

            const Card(
              child: Padding(
                padding:
                    EdgeInsets.all(
                  14,
                ),
                child: Text(
                  'Safety: this application is for '
                  'AI-assisted research and paper trading. '
                  'Historical win rates, fast scores, '
                  'deep-validation results and model '
                  'outputs do not guarantee future profit.',
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
