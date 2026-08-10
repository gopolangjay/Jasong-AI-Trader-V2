import 'dart:async';
import 'dart:convert';
import 'dart:io';

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
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  // =========================================================
  // USER SETTINGS
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
  // APP STATE
  // =========================================================

  bool busy = false;
  bool scanningMarkets = false;
  bool findingVerifiedTrade = false;

  String? currentValidationMarket;
  String? networkStatus;
  String? error;

  int currentAttempt = 0;
  int maximumAttempts = 3;

  // =========================================================
  // GENERIC GET
  // =========================================================

  Future<Map<String, dynamic>> getJson(
    Uri uri, {
    int timeoutSeconds = 120,
  }) async {
    final client = http.Client();

    try {
      final response = await client
          .get(
            uri,
            headers: {
              'Accept': 'application/json',
              'Connection': 'close',
            },
          )
          .timeout(
            Duration(
              seconds: timeoutSeconds,
            ),
          );

      if (response.statusCode != 200) {
        throw HttpException(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );
      }

      final decoded = jsonDecode(
        response.body,
      );

      if (decoded is! Map) {
        throw const FormatException(
          'Unexpected JSON response',
        );
      }

      return Map<String, dynamic>.from(
        decoded,
      );
    } finally {
      client.close();
    }
  }

  // =========================================================
  // CHECK WHETHER ERROR IS NETWORK RELATED
  // =========================================================

  bool isNetworkError(
    Object error,
  ) {
    if (error is SocketException) {
      return true;
    }

    if (error is TimeoutException) {
      return true;
    }

    if (error is http.ClientException) {
      return true;
    }

    final text =
        error.toString().toLowerCase();

    return text.contains(
          'socketexception',
        ) ||
        text.contains(
          'clientexception',
        ) ||
        text.contains(
          'failed host lookup',
        ) ||
        text.contains(
          'connection abort',
        ) ||
        text.contains(
          'connection reset',
        ) ||
        text.contains(
          'connection closed',
        ) ||
        text.contains(
          'timed out',
        ) ||
        text.contains(
          'timeout',
        ) ||
        text.contains(
          'http 502',
        ) ||
        text.contains(
          'http 503',
        ) ||
        text.contains(
          'http 504',
        );
  }

  // =========================================================
  // HEALTH CHECK
  // =========================================================

  Future<bool> checkBackendHealth() async {
    final uri = Uri.parse(
      '$apiBase/health',
    );

    try {
      final client = http.Client();

      try {
        final response = await client
            .get(
              uri,
              headers: {
                'Accept':
                    'application/json',
                'Connection':
                    'close',
              },
            )
            .timeout(
              const Duration(
                seconds: 35,
              ),
            );

        return response.statusCode == 200;
      } finally {
        client.close();
      }
    } catch (_) {
      return false;
    }
  }

  // =========================================================
  // WAIT FOR BACKEND TO RECOVER
  // =========================================================

  Future<bool> waitForBackendRecovery(
    String market,
    int attempt,
  ) async {
    const delays = [
      8,
      12,
      18,
    ];

    final delayIndex =
        (attempt - 1).clamp(
      0,
      delays.length - 1,
    );

    final delay =
        delays[delayIndex];

    if (mounted) {
      setState(() {
        currentValidationMarket =
            'Connection interrupted';

        networkStatus =
            'Waiting ${delay}s before '
            'retrying $market...';
      });
    }

    await Future.delayed(
      Duration(
        seconds: delay,
      ),
    );

    if (mounted) {
      setState(() {
        networkStatus =
            'Checking Jasong AI '
            'Trader server...';
      });
    }

    bool healthy =
        await checkBackendHealth();

    if (healthy) {
      if (mounted) {
        setState(() {
          networkStatus =
              'Server online. '
              'Retrying $market...';
        });
      }

      await Future.delayed(
        const Duration(
          seconds: 2,
        ),
      );

      return true;
    }

    // Give Render one more chance to wake.
    if (mounted) {
      setState(() {
        networkStatus =
            'Server still waking. '
            'Checking again...';
      });
    }

    await Future.delayed(
      const Duration(
        seconds: 10,
      ),
    );

    healthy =
        await checkBackendHealth();

    return healthy;
  }

  // =========================================================
  // RESILIENT POST
  // =========================================================

  Future<Map<String, dynamic>>
      postJsonOnce(
    Uri uri,
    dynamic body, {
    int timeoutSeconds = 240,
  }) async {
    final client = http.Client();

    try {
      final response = await client
          .post(
            uri,
            headers: {
              'Accept':
                  'application/json',
              'Content-Type':
                  'application/json',
              'Connection':
                  'close',
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
        throw HttpException(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );
      }

      final decoded = jsonDecode(
        response.body,
      );

      if (decoded is! Map) {
        throw const FormatException(
          'Backend returned an '
          'unexpected JSON response',
        );
      }

      return Map<String, dynamic>.from(
        decoded,
      );
    } finally {
      client.close();
    }
  }

  // =========================================================
  // V4.8 ASYNC DEEP VALIDATION JOB
  // =========================================================

  Future<Map<String, dynamic>>
      deepValidateWithRecovery(
    Map<String, dynamic> candidate,
  ) async {
    final market =
        candidate['market']
                ?.toString() ??
            'UNKNOWN';

    final createUri = Uri.parse(
      '$apiBase/deep-validation-job',
    ).replace(
      queryParameters: {
        'risk_mode':
            risk,
        'starting_balance':
            balance.text.trim(),
        'payout':
            '0.8',
      },
    );

    // V4.8 job endpoint accepts ONE candidate object,
    // not the old one-item list used by /deep-validate.
    final body = {
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
    };

    Object? lastError;
    String? jobId;

    // ---------------------------------------------------------
    // 1. CREATE THE BACKGROUND JOB
    // ---------------------------------------------------------

    for (
      int attempt = 1;
      attempt <= maximumAttempts;
      attempt++
    ) {
      if (!mounted) {
        throw Exception(
          'App closed during validation',
        );
      }

      setState(() {
        currentAttempt =
            attempt;

        currentValidationMarket =
            'Starting deep validation for $market';

        networkStatus =
            'Creating validation job '
            '(attempt $attempt/$maximumAttempts)...';
      });

      try {
        final created =
            await postJsonOnce(
          createUri,
          body,
          timeoutSeconds: 60,
        );

        jobId =
            created['job_id']
                ?.toString();

        if (jobId == null ||
            jobId.isEmpty) {
          throw const FormatException(
            'Validation server did not return a job_id',
          );
        }

        if (mounted) {
          setState(() {
            currentAttempt = 0;
            currentValidationMarket =
                'Deep validating $market';

            networkStatus =
                'Validation job queued. '
                'Waiting for the server...';
          });
        }

        break;
      } catch (e) {
        lastError = e;

        if (!isNetworkError(e)) {
          rethrow;
        }

        if (attempt >=
            maximumAttempts) {
          break;
        }

        await waitForBackendRecovery(
          market,
          attempt,
        );
      }
    }

    if (jobId == null ||
        jobId.isEmpty) {
      throw NetworkValidationException(
        market: market,
        message:
            lastError?.toString() ??
                'Could not create validation job',
      );
    }

    // ---------------------------------------------------------
    // 2. POLL THE JOB
    //
    // Deep validation can take several minutes. Each GET is
    // short; a temporary network failure does NOT reject the
    // candidate and does NOT create a second validation job.
    // ---------------------------------------------------------

    final pollUri = Uri.parse(
      '$apiBase/deep-validation-job/$jobId',
    );

    const pollDelay =
        Duration(seconds: 5);

    const maxPollingTime =
        Duration(minutes: 12);

    final stopwatch =
        Stopwatch()..start();

    int consecutiveNetworkErrors = 0;

    while (
      stopwatch.elapsed <
          maxPollingTime
    ) {
      if (!mounted) {
        throw Exception(
          'App closed during validation',
        );
      }

      try {
        final job =
            await getJson(
          pollUri,
          timeoutSeconds: 45,
        );

        consecutiveNetworkErrors = 0;

        final status =
            job['status']
                    ?.toString()
                    .toUpperCase() ??
                'UNKNOWN';

        if (status == 'COMPLETED') {
          stopwatch.stop();

          final rawResult =
              job['result'];

          if (rawResult is! Map) {
            throw const FormatException(
              'Completed validation job '
              'did not contain a result',
            );
          }

          final result =
              Map<String, dynamic>.from(
            rawResult,
          );

          if (mounted) {
            setState(() {
              currentValidationMarket =
                  '$market validation complete';

              networkStatus =
                  'Deep validation result received';
            });
          }

          return result;
        }

        if (status == 'FAILED' ||
            status == 'ERROR') {
          stopwatch.stop();

          final jobError =
              job['error']
                      ?.toString() ??
                  'Deep validation job failed';

          throw Exception(
            jobError,
          );
        }

        final elapsedSeconds =
            stopwatch.elapsed.inSeconds;

        if (mounted) {
          setState(() {
            currentValidationMarket =
                'Deep validating $market';

            networkStatus =
                '$status • '
                '${elapsedSeconds}s elapsed • '
                'checking again in '
                '${pollDelay.inSeconds}s';
          });
        }

        await Future.delayed(
          pollDelay,
        );
      } catch (e) {
        if (!isNetworkError(e)) {
          rethrow;
        }

        consecutiveNetworkErrors++;

        if (mounted) {
          setState(() {
            currentValidationMarket =
                'Deep validating $market';

            networkStatus =
                'Connection interrupted while '
                'checking the job. '
                'The server job is still running. '
                'Retrying...';
          });
        }

        // Keep the SAME job_id. Never restart the validation
        // merely because one polling request lost connection.
        final retryDelay =
            Duration(
          seconds:
              consecutiveNetworkErrors >= 3
                  ? 15
                  : 8,
        );

        await Future.delayed(
          retryDelay,
        );
      }
    }

    stopwatch.stop();

    throw NetworkValidationException(
      market: market,
      message:
          'Validation job $jobId did not finish '
          'within ${maxPollingTime.inMinutes} minutes. '
          'The job may still be running on the server.',
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
  // FAST SCAN REQUEST
  // =========================================================

  Future<Map<String, dynamic>>
      runFastScanRequest() async {
    final uri = Uri.parse(
      '$apiBase/fast-scan',
    ).replace(
      queryParameters: {
        'period': '5d',
        'interval': '15m',
        'top_n': '3',
      },
    );

    return getJson(
      uri,
      timeoutSeconds: 180,
    );
  }

  // =========================================================
  // FAST SCAN BUTTON
  // =========================================================

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

      networkStatus = null;

      currentAttempt = 0;

      currentValidationMarket =
          'Scanning all markets...';
    });

    try {
      // =====================================================
      // 1. FAST SCAN
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
          'Fast scanner returned '
          'no candidates',
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
          'No valid candidate '
          'records received',
        );
      }

      final count =
          candidates.length > 3
              ? 3
              : candidates.length;

      // =====================================================
      // 2. DEEP VALIDATE EACH MARKET
      // =====================================================

      for (
        int index = 0;
        index < count;
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
              '#${index + 1} $market';

          networkStatus =
              'Preparing validation...';
        });

        Map<String, dynamic> deep;

        try {
          deep =
              await deepValidateWithRecovery(
            candidate,
          );
        } on NetworkValidationException catch (e) {
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
                  'NETWORK_ERROR',

              'verified':
                  false,

              'error':
                  e.message,
            });

            currentValidationMarket =
                '$market validation '
                'could not complete';

            networkStatus =
                'Network connection failed '
                'after $maximumAttempts attempts.';
          });

          // IMPORTANT:
          // Do NOT move to candidate #2 after a
          // network failure.
          throw NetworkValidationException(
            market:
                market,
            message:
                e.message,
          );
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

          // Non-network backend error:
          // stop rather than falsely treating the
          // candidate as rejected.
          break;
        }

        // ===================================================
        // PARSE V4.6 RESPONSE
        // ===================================================

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

        final verified =
            finalMarket?[
                        'verified'] ==
                    true ||
                finalStatus ==
                    'VERIFIED_TRADE';

        final deepStatus =
            finalMarket?[
                        'status']
                    ?.toString() ??
                finalStatus;

        final history = <
            String, dynamic>{
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

          'sample_reliability_pct':
              finalMarket?[
                  'sample_reliability_pct'],

          'wilson_lower_win_rate_pct':
              finalMarket?[
                  'wilson_lower_win_rate_pct'],

          'reliability_adjusted_score':
              finalMarket?[
                  'reliability_adjusted_score'],

          'near_verified':
              finalMarket?[
                  'near_verified'] == true,

          'primary_reason':
              finalMarket?[
                  'primary_reason'],

          'rejection_reasons':
              finalMarket?[
                  'rejection_reasons'],

          'explanation':
              finalMarket?[
                  'explanation'],
        };

        if (!mounted) {
          return;
        }

        setState(() {
          validationHistory.add(
            history,
          );

          networkStatus =
              '$market validation complete';
        });

        // ===================================================
        // NOT VERIFIED
        // ===================================================

        if (!verified ||
            finalMarket == null) {
          continue;
        }

        // ===================================================
        // DIRECTION AGREEMENT
        // ===================================================

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

        // ===================================================
        // VERIFIED
        // ===================================================

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

        setState(() {
          verifiedTrade =
              result;

          currentValidationMarket =
              '$market VERIFIED';

          networkStatus =
              'Deep validation passed';
        });

        break;
      }
    } on NetworkValidationException catch (_) {
      // Already shown in the validation card.
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
  // ANALYSE MARKET
  // =========================================================

  Future<void> analyseMarket(
    Map<String, dynamic> market,
  ) async {
    final selected =
        market['symbol']
            ?.toString();

    if (selected == null ||
        selected.isEmpty) {
      return;
    }

    setState(() {
      symbol.text =
          selected;

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

      final response =
          await http
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
          content:
              Text(
            'Paper trade recorded',
          ),
        ),
      );
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            e.toString();
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
  // FORMATTERS
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
      case 'VERIFIED_TRADE':
      case 'STRONG':
        return Colors.greenAccent;

      case 'NEAR_VERIFIED':
        return Colors.amberAccent;

      case 'WATCH':
        return Colors.amberAccent;

      case 'NETWORK_ERROR':
        return Colors.orangeAccent;

      case 'DIRECTION_MISMATCH':
        return Colors.orangeAccent;

      case 'REJECT':
      case 'ERROR':
        return Colors.redAccent;

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

  // =========================================================
  // METRIC
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

    final nearVerified =
        item['near_verified'] == true ||
            status == 'NEAR_VERIFIED';

    final errorMessage =
        item['error']
            ?.toString();

    final explanation =
        item['explanation']
            ?.toString();

    final primaryReason =
        item['primary_reason']
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
                      : nearVerified
                          ? Icons
                              .verified_outlined
                          : status ==
                                  'NETWORK_ERROR'
                              ? Icons.wifi_off
                              : Icons
                                  .cancel_outlined,
                  color:
                      verified
                          ? Colors
                              .greenAccent
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
              status.replaceAll(
                '_',
                ' ',
              ),
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

            const SizedBox(
              height: 8,
            ),

            Row(
              children: [
                Expanded(
                  child: Text(
                    'Deep score: '
                    '${formatNumber(item['deep_score'])}',
                  ),
                ),
                Expanded(
                  child: Text(
                    'Reliable score: '
                    '${formatNumber(item['reliability_adjusted_score'])}',
                  ),
                ),
              ],
            ),

            const SizedBox(
              height: 4,
            ),

            Text(
              'Historical: '
              '${item['wins'] ?? '-'} wins / '
              '${item['losses'] ?? '-'} losses '
              '(${formatPercent(item['win_rate'])}% WR)',
            ),

            Text(
              'Trades: ${item['trades'] ?? '-'}'
              ' • PF ${formatNumber(item['profit_factor'])}'
              ' • Max DD '
              '${formatPercent(item['max_drawdown'])}%',
            ),

            Text(
              'Reliability: '
              '${formatNumber(item['sample_reliability_pct'], decimals: 1)}%'
              ' • Conservative WR: '
              '${formatNumber(item['wilson_lower_win_rate_pct'], decimals: 1)}%',
            ),

            if (item['interval'] != null ||
                item['period'] != null)
              Text(
                'Setup: '
                '${item['interval'] ?? '-'}'
                ' • ${item['period'] ?? '-'}'
                ' • Hold '
                '${item['holding_candles'] ?? '-'} candles',
              ),

            if (primaryReason != null &&
                primaryReason.isNotEmpty) ...[
              const SizedBox(
                height: 8,
              ),
              Text(
                'Reason: '
                '${primaryReason.replaceAll('_', ' ')}',
                style:
                    TextStyle(
                  color:
                      statusColor(
                    status,
                  ),
                  fontWeight:
                      FontWeight.bold,
                ),
              ),
            ],

            if (explanation != null &&
                explanation.isNotEmpty) ...[
              const SizedBox(
                height: 8,
              ),
              Text(
                explanation,
                style:
                    const TextStyle(
                  color:
                      Colors.white70,
                  fontSize: 12,
                ),
              ),
            ],

            if (errorMessage != null) ...[
              const SizedBox(
                height: 10,
              ),
              Text(
                errorMessage,
                style:
                    const TextStyle(
                  fontSize: 12,
                  color:
                      Colors.orangeAccent,
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

    final rawScore =
        market['fast_score'] ??
            0.0;

    final score =
        rawScore is num
            ? rawScore.toDouble()
            : 0.0;

    final reasons =
        (market['reasons']
                    as List?) ??
            const [];

    return Card(
      child: InkWell(
        onTap:
            busy
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
                height: 8,
              ),

              Row(
                children: [
                  Text(
                    status,
                    style:
                        TextStyle(
                      color:
                          statusColor(
                        status,
                      ),
                      fontWeight:
                          FontWeight.bold,
                    ),
                  ),

                  const Spacer(),

                  Text(
                    'Fast Score '
                    '${score.toStringAsFixed(1)}/100',
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
  // BUILD
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

    final ranking =
        (fastScan?['ranking']
                    as List?) ??
            const [];

    final hasNetworkFailure =
        validationHistory.any(
      (item) =>
          item['deep_status'] ==
          'NETWORK_ERROR',
    );

    final hasBackendFailure =
        validationHistory.any(
      (item) =>
          item['deep_status'] ==
          'ERROR',
    );

    return Scaffold(
      appBar: AppBar(
        title:
            const Text(
          'Jasong AI Trader V5.0',
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

            // =================================================
            // NETWORK RECOVERY UI
            // =================================================

            if (findingVerifiedTrade)
              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    18,
                  ),
                  child: Column(
                    children: [
                      const LinearProgressIndicator(),

                      const SizedBox(
                        height: 14,
                      ),

                      Text(
                        currentValidationMarket ??
                            'Working...',
                        textAlign:
                            TextAlign.center,
                        style:
                            const TextStyle(
                          fontSize: 18,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      if (currentAttempt > 0) ...[
                        const SizedBox(
                          height: 8,
                        ),

                        Text(
                          'Attempt '
                          '$currentAttempt/'
                          '$maximumAttempts',
                          style:
                              const TextStyle(
                            color:
                                Colors.tealAccent,
                            fontWeight:
                                FontWeight.bold,
                          ),
                        ),
                      ],

                      if (networkStatus != null) ...[
                        const SizedBox(
                          height: 8,
                        ),

                        Text(
                          networkStatus!,
                          textAlign:
                              TextAlign.center,
                        ),
                      ],

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'Deep validation now runs as a '
                        'background server job. The app checks '
                        'the same job until it completes. '
                        'A temporary network error will not be '
                        'treated as a rejected trade.',
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
            // VERIFIED TRADE
            // =================================================

            if (verifiedTrade != null) ...[
              const SizedBox(
                height: 20,
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
                        size: 54,
                        color:
                            Colors.greenAccent,
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'VERIFIED TRADE',
                        style:
                            TextStyle(
                          color:
                              Colors.greenAccent,
                          fontSize: 18,
                          fontWeight:
                              FontWeight.w900,
                        ),
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      Row(
                        mainAxisAlignment:
                            MainAxisAlignment
                                .center,
                        children: [
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
                            width: 16,
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
                        ],
                      ),

                      const SizedBox(
                        height: 14,
                      ),

                      Row(
                        children: [
                          metric(
                            'Deep Score',
                            formatNumber(
                              verifiedTrade![
                                  'deep_score'],
                            ),
                          ),
                          metric(
                            'Historical WR',
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
                            'Reliability',
                            '${formatNumber(
                              verifiedTrade![
                                  'sample_reliability_pct'],
                              decimals: 1,
                            )}%',
                          ),
                          metric(
                            'Conservative WR',
                            '${formatNumber(
                              verifiedTrade![
                                  'wilson_lower_win_rate_pct'],
                              decimals: 1,
                            )}%',
                          ),
                        ],
                      ),

                      Row(
                        children: [
                          metric(
                            'Wins / Losses',
                            '${verifiedTrade!['wins'] ?? '-'} / '
                            '${verifiedTrade!['losses'] ?? '-'}',
                          ),
                          metric(
                            'Trades',
                            '${verifiedTrade!['trades'] ?? '-'}',
                          ),
                        ],
                      ),

                      Row(
                        children: [
                          metric(
                            'Profit Factor',
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
                            'Backtest Return',
                            '${formatPercent(
                              verifiedTrade![
                                  'return_pct'],
                            )}%',
                          ),
                          metric(
                            'Fast Score',
                            '${formatNumber(
                              verifiedTrade![
                                  'fast_score'],
                              decimals: 1,
                            )}/100',
                          ),
                        ],
                      ),

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
                            children: [
                              Text(
                                'TIMEFRAME  '
                                '${verifiedTrade!['interval'] ?? '-'}',
                                style:
                                    const TextStyle(
                                  fontWeight:
                                      FontWeight.bold,
                                ),
                              ),
                              const SizedBox(
                                height: 4,
                              ),
                              Text(
                                'HISTORICAL WINDOW  '
                                '${verifiedTrade!['period'] ?? '-'}',
                              ),
                              const SizedBox(
                                height: 4,
                              ),
                              Text(
                                'HOLDING WINDOW  '
                                '${verifiedTrade!['holding_candles'] ?? '-'} candles',
                              ),
                              const SizedBox(
                                height: 4,
                              ),
                              Text(
                                'ENTRY THRESHOLD  '
                                '${verifiedTrade!['threshold_pct'] ?? '-'}%',
                              ),
                            ],
                          ),
                        ),
                      ),

                      const SizedBox(
                        height: 12,
                      ),

                      const Align(
                        alignment:
                            Alignment.centerLeft,
                        child: Text(
                          'Validation checks',
                          style:
                              TextStyle(
                            fontWeight:
                                FontWeight.bold,
                            fontSize: 16,
                          ),
                        ),
                      ),

                      const SizedBox(
                        height: 6,
                      ),

                      const ListTile(
                        dense: true,
                        leading: Icon(
                          Icons.check_circle,
                          color:
                              Colors.greenAccent,
                        ),
                        title: Text(
                          'Sample size passed',
                        ),
                      ),
                      const ListTile(
                        dense: true,
                        leading: Icon(
                          Icons.check_circle,
                          color:
                              Colors.greenAccent,
                        ),
                        title: Text(
                          'Historical win rate passed',
                        ),
                      ),
                      const ListTile(
                        dense: true,
                        leading: Icon(
                          Icons.check_circle,
                          color:
                              Colors.greenAccent,
                        ),
                        title: Text(
                          'Profit factor passed',
                        ),
                      ),
                      const ListTile(
                        dense: true,
                        leading: Icon(
                          Icons.check_circle,
                          color:
                              Colors.greenAccent,
                        ),
                        title: Text(
                          'Drawdown limit passed',
                        ),
                      ),
                      const ListTile(
                        dense: true,
                        leading: Icon(
                          Icons.check_circle,
                          color:
                              Colors.greenAccent,
                        ),
                        title: Text(
                          'Validated sample passed',
                        ),
                      ),

                      if (verifiedTrade![
                              'explanation'] !=
                          null) ...[
                        const SizedBox(
                          height: 8,
                        ),
                        Text(
                          '${verifiedTrade!['explanation']}',
                          textAlign:
                              TextAlign.center,
                          style:
                              const TextStyle(
                            color:
                                Colors.white70,
                            fontSize: 12,
                          ),
                        ),
                      ],

                      const SizedBox(
                        height: 14,
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

                      OutlinedButton.icon(
                        onPressed:
                            busy ||
                                    sig == null ||
                                    ![
                                      'BUY',
                                      'SELL',
                                    ].contains(
                                      sig![
                                              'decision']
                                          ?.toString(),
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

                      const SizedBox(
                        height: 10,
                      ),

                      const Text(
                        'VERIFIED means the historical setup '
                        'passed the configured validation rules. '
                        'Historical win rate and scores are not '
                        'guarantees of the next trade outcome.',
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
            // NETWORK FAILURE
            // =================================================

            if (!findingVerifiedTrade &&
                verifiedTrade == null &&
                hasNetworkFailure)
              Card(
                child: Padding(
                  padding:
                      const EdgeInsets.all(
                    18,
                  ),
                  child: Column(
                    children: [
                      const Icon(
                        Icons.wifi_off,
                        size: 46,
                        color:
                            Colors.orangeAccent,
                      ),

                      const SizedBox(
                        height: 10,
                      ),

                      const Text(
                        'NETWORK VALIDATION ERROR',
                        style:
                            TextStyle(
                          fontSize: 20,
                          fontWeight:
                              FontWeight.bold,
                          color:
                              Colors.orangeAccent,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'The trade was NOT rejected. '
                        'The connection to the validation '
                        'server failed after retries. '
                        'Run Find Verified Trade again '
                        'when the connection is stable.',
                        textAlign:
                            TextAlign.center,
                      ),
                    ],
                  ),
                ),
              ),

            // =================================================
            // BACKEND FAILURE
            // =================================================

            if (!findingVerifiedTrade &&
                verifiedTrade == null &&
                hasBackendFailure &&
                !hasNetworkFailure)
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
                        size: 46,
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
                          color:
                              Colors.redAccent,
                          fontSize: 20,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'Deep validation encountered '
                        'an application error. '
                        'See the details below.',
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
                !hasNetworkFailure &&
                !hasBackendFailure)
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
                        size: 44,
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
                          fontSize: 21,
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),

                      const SizedBox(
                        height: 8,
                      ),

                      const Text(
                        'The tested candidates did '
                        'not satisfy deep-validation '
                        'requirements.',
                        textAlign:
                            TextAlign.center,
                      ),
                    ],
                  ),
                ),
              ),

            // =================================================
            // VALIDATION HISTORY
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
            // FAST SCANNER
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

              if (ranking.isNotEmpty)
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
                            Text(
                          '#${i + 1}',
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
              height: 20,
            ),

            const Card(
              child: Padding(
                padding:
                    EdgeInsets.all(
                  14,
                ),
                child: Text(
                  'Safety: Jasong AI Trader is for '
                  'AI-assisted analysis and paper trading. '
                  'Fast Score is a ranking score, not a win probability. '
                  'A VERIFIED result means historical validation rules '
                  'passed; it does not guarantee the next trade will win.',
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


// ===========================================================
// CUSTOM NETWORK EXCEPTION
// ===========================================================

class NetworkValidationException
    implements Exception {
  final String market;
  final String message;

  NetworkValidationException({
    required this.market,
    required this.message,
  });

  @override
  String toString() {
    return (
      'Network validation failed '
      'for $market: $message'
    );
  }
}
