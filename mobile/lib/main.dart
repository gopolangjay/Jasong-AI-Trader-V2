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
        useMaterial3: true,
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF07111A),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF65E6D3),
          brightness: Brightness.dark,
          primary: const Color(0xFF65E6D3),
          secondary: const Color(0xFF6FA8FF),
          surface: const Color(0xFF0E1A24),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.transparent,
          elevation: 0,
          scrolledUnderElevation: 0,
        ),
        cardTheme: CardThemeData(
          color: const Color(0xFF0E1A24),
          elevation: 0,
          margin: EdgeInsets.zero,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(22),
            side: const BorderSide(
              color: Color(0xFF17303A),
            ),
          ),
        ),
        navigationBarTheme: NavigationBarThemeData(
          backgroundColor: const Color(0xFF0A151E),
          indicatorColor: const Color(0xFF65E6D3).withValues(alpha: .16),
          labelTextStyle: WidgetStateProperty.resolveWith((states) {
            return TextStyle(
              fontSize: 11,
              fontWeight: states.contains(WidgetState.selected)
                  ? FontWeight.w800
                  : FontWeight.w500,
              color: states.contains(WidgetState.selected)
                  ? const Color(0xFF65E6D3)
                  : Colors.white54,
            );
          }),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: const Color(0xFF0B1720),
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(color: Color(0xFF24404B)),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(color: Color(0xFF24404B)),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(16),
            borderSide: const BorderSide(color: Color(0xFF65E6D3)),
          ),
        ),
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

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
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

  int selectedTab = 0;

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

  Map<String, dynamic>? liveEntryAssessment;

  Map<String, dynamic>? serverWatcher;
  List<Map<String, dynamic>> serverWatchers = [];
  Map<String, dynamic>? forwardStats;
  Map<String, dynamic>? v66ForwardIntelligence;

  List<Map<String, dynamic>> paperTrades = [];

  // V6.6.2 autonomous AI PAPER-learning monitor.
  Map<String, dynamic>? aiLearningStatus;
  Map<String, dynamic>? aiLearningSnapshot;
  List<Map<String, dynamic>> aiLearningWatchers = [];
  Map<String, dynamic>? aiLearningLastRun;
  bool aiLearningBusy = false;

  // V6.6.4 mobile control plane for server-side IG DEMO overnight runs.
  Map<String, dynamic>? overnightDemoStatus;
  Map<String, dynamic>? igDemoPerformance;
  bool overnightDemoBusy = false;

  final TextEditingController copilotController = TextEditingController();
  bool copilotBusy = false;
  String copilotAnswer = '';

  Map<String, dynamic>? systemOverview;
  Map<String, dynamic>? systemDiagnostic;
  bool systemDiagnosticBusy = false;

  Map<String, dynamic>? autoDashboard;
  Map<String, dynamic>? autoManagerJob;

  Timer? watcherPollTimer;
  Timer? autoDashboardPollTimer;

  bool watcherBusy = false;
  bool autoManagerBusy = false;

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
        'top_n': '9',
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

      liveEntryAssessment = null;
      serverWatcher = null;
      serverWatchers = [];
      forwardStats = null;
      watcherPollTimer?.cancel();

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
          verifiedTrade ??=
              result;

          currentValidationMarket =
              '$market VERIFIED';

          networkStatus =
              'Deep validation passed • '
              'adding candidate to V5.4 watch portfolio';
        });

        await createServerWatcher(
          result,
        );

        // V5.4 intentionally continues through the remaining
        // shortlisted candidates. Multiple VERIFIED setups can
        // be watched simultaneously, so a waiting/overextended
        // #1 candidate does not block a better live entry in #2/#3.
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
  // V5.5.3 AUTO MANAGER / FORWARD LIFECYCLE
  // =========================================================

  Future<void> askJasongCopilot({
    String? presetQuestion,
    String mode = 'GENERAL',
  }) async {
    final question = (presetQuestion ?? copilotController.text).trim();
    if (question.isEmpty) return;
    setState(() {
      copilotBusy = true;
      copilotAnswer = '';
    });
    try {
      final response = await postJsonOnce(
        Uri.parse('$apiBase/v68/ask'),
        {
          'question': question,
          'mode': mode,
        },
        timeoutSeconds: 90,
      );
      if (!mounted) return;
      setState(() {
        copilotAnswer = response['answer']?.toString() ?? 'No analysis returned.';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        copilotAnswer = 'Copilot error: $e';
      });
    } finally {
      if (mounted) setState(() => copilotBusy = false);
    }
  }

  Future<void> runOvernightReview() async {
    setState(() {
      copilotBusy = true;
      copilotAnswer = '';
    });
    try {
      final response = await getJson(
        Uri.parse('$apiBase/v68/overnight-review'),
        timeoutSeconds: 90,
      );
      if (!mounted) return;
      setState(() {
        copilotAnswer = response['answer']?.toString() ?? 'No overnight analysis returned.';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        copilotAnswer = 'Copilot error: $e';
      });
    } finally {
      if (mounted) setState(() => copilotBusy = false);
    }
  }

  Future<void> loadSystemOverview() async {
    try {
      final response = await getJson(
        Uri.parse(
          '$apiBase/v69/system-overview',
        ),
        timeoutSeconds: 45,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        systemOverview = response;
      });
    } catch (_) {
      // Keep the previous snapshot visible if a single poll fails.
    }
  }

  Future<void> runSystemDiagnostic() async {
    if (systemDiagnosticBusy) {
      return;
    }

    setState(() {
      systemDiagnosticBusy = true;
      systemDiagnostic = null;
    });

    try {
      final response = await getJson(
        Uri.parse(
          '$apiBase/v69/diagnostic',
        ),
        timeoutSeconds: 90,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        systemDiagnostic = response;
      });

      await loadSystemOverview();
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        systemDiagnostic = {
          'status': 'RED',
          'label': 'DIAGNOSTIC REQUEST FAILED',
          'checks': [
            {
              'component': 'MOBILE_TO_BACKEND',
              'passed': false,
              'message': e.toString(),
            }
          ],
        };
      });
    } finally {
      if (mounted) {
        setState(() {
          systemDiagnosticBusy = false;
        });
      }
    }
  }

  Color systemStatusColor(
    String status,
  ) {
    switch (status.toUpperCase()) {
      case 'GREEN':
        return Colors.greenAccent;
      case 'AMBER':
        return Colors.amberAccent;
      case 'RED':
        return Colors.redAccent;
      case 'GREY':
      default:
        return Colors.white54;
    }
  }

  IconData systemStatusIcon(
    String status,
  ) {
    switch (status.toUpperCase()) {
      case 'GREEN':
        return Icons.check_circle;
      case 'AMBER':
        return Icons.warning_amber_rounded;
      case 'RED':
        return Icons.error;
      case 'GREY':
      default:
        return Icons.pause_circle;
    }
  }

  String formatCountdownSeconds(
    dynamic value,
  ) {
    final seconds = double.tryParse(
      value?.toString() ?? '',
    );

    if (seconds == null) {
      return '-';
    }

    if (seconds <= 0) {
      return 'Due now';
    }

    final total = seconds.round();
    final minutes = total ~/ 60;
    final remainder = total % 60;

    if (minutes > 0) {
      return '${minutes}m ${remainder}s';
    }

    return '${remainder}s';
  }

  String formatEpochCountdown(
    dynamic value,
  ) {
    final epoch = double.tryParse(
      value?.toString() ?? '',
    );

    if (epoch == null || epoch <= 0) {
      return '-';
    }

    final now =
        DateTime.now().millisecondsSinceEpoch /
            1000.0;

    return formatCountdownSeconds(
      epoch - now,
    );
  }

  Future<void> loadOvernightDemoStatus() async {
    try {
      final response = await getJson(
        Uri.parse(
          '$apiBase/overnight-demo/status',
        ),
        timeoutSeconds: 45,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        overnightDemoStatus = response;

        final rawPerformance =
            response['broker_performance'];

        if (rawPerformance is Map) {
          igDemoPerformance =
              Map<String, dynamic>.from(
            rawPerformance,
          );
        }
      });
    } catch (_) {
      // Keep the last server-side overnight snapshot visible.
    }
  }

  Future<void> startOvernightDemo() async {
    if (overnightDemoBusy) {
      return;
    }

    setState(() {
      overnightDemoBusy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/overnight-demo/start',
      ).replace(
        queryParameters: {
          'risk_mode': risk,
          'starting_balance':
              balance.text.trim(),
          'payout': '0.8',
        },
      );

      final response = await postJsonOnce(
        uri,
        <String, dynamic>{},
        timeoutSeconds: 90,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        overnightDemoStatus = response;

        final rawPerformance =
            response['broker_performance'];
        if (rawPerformance is Map) {
          igDemoPerformance =
              Map<String, dynamic>.from(
            rawPerformance,
          );
        }

        networkStatus =
            'Overnight IG DEMO is running on the server.';
      });

      startAutoDashboardPolling();
      await loadAutoDashboard();
      await loadAiLearningStatus();
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Could not start Overnight DEMO: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          overnightDemoBusy = false;
        });
      } else {
        overnightDemoBusy = false;
      }
    }
  }

  Future<void> stopOvernightDemo() async {
    if (overnightDemoBusy) {
      return;
    }

    setState(() {
      overnightDemoBusy = true;
      error = null;
    });

    try {
      final response = await postJsonOnce(
        Uri.parse(
          '$apiBase/overnight-demo/stop',
        ),
        <String, dynamic>{},
        timeoutSeconds: 90,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        overnightDemoStatus = response;

        final rawPerformance =
            response['broker_performance'];
        if (rawPerformance is Map) {
          igDemoPerformance =
              Map<String, dynamic>.from(
            rawPerformance,
          );
        }

        networkStatus = response['message']
                ?.toString() ??
            'Overnight DEMO stopped.';
      });

      await loadAutoDashboard();
      await loadAiLearningStatus();
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Could not stop Overnight DEMO: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          overnightDemoBusy = false;
        });
      } else {
        overnightDemoBusy = false;
      }
    }
  }

  Future<void> loadAiLearningStatus() async {
    try {
      final response = await getJson(
        Uri.parse(
          '$apiBase/ai-learning/status',
        ),
        timeoutSeconds: 45,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        aiLearningStatus = response;

        final learning =
            response['learning'];

        if (learning is Map) {
          aiLearningSnapshot =
              Map<String, dynamic>.from(
            learning,
          );
        }
      });
    } catch (_) {
      // Keep the previous AI-learning snapshot visible.
    }
  }

  Future<void> runAiLearningNow() async {
    if (aiLearningBusy) {
      return;
    }

    setState(() {
      aiLearningBusy = true;
      error = null;
    });

    try {
      final response = await postJsonOnce(
        Uri.parse(
          '$apiBase/ai-learning/run-now',
        ),
        <String, dynamic>{},
        timeoutSeconds: 90,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        aiLearningLastRun = response;
      });

      await loadAutoDashboard();
      await loadAiLearningStatus();
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        aiLearningLastRun = {
          'status': 'ERROR',
          'paper_only': true,
          'live_execution': false,
          'error': e.toString(),
        };
      });
    } finally {
      if (mounted) {
        setState(() {
          aiLearningBusy = false;
        });
      } else {
        aiLearningBusy = false;
      }
    }
  }

  Future<void> loadAutoDashboard() async {
    try {
      final uri = Uri.parse(
        '$apiBase/auto-dashboard',
      ).replace(
        queryParameters: {
          'starting_balance':
              balance.text.trim(),
        },
      );

      final response = await getJson(
        uri,
        timeoutSeconds: 45,
      );

      if (!mounted) {
        return;
      }

      setState(() {
        autoDashboard = response;

        final rawWatchers =
            response['lifecycle'];

        if (rawWatchers is List) {
          serverWatchers = rawWatchers
              .whereType<Map>()
              .map(
                (item) =>
                    Map<String, dynamic>.from(
                  item,
                ),
              )
              .toList();

          serverWatcher =
              _selectPrimaryWatcher(
            serverWatchers,
          );
        }

        final rawForward =
            response['forward'];

        if (rawForward is Map) {
          forwardStats =
              Map<String, dynamic>.from(
            rawForward,
          );
        }

        final rawPaperTrades =
            response['paper_trades'];

        if (rawPaperTrades is List) {
          paperTrades =
              rawPaperTrades
                  .whereType<Map>()
                  .map(
                    (item) =>
                        Map<String, dynamic>.from(
                      item,
                    ),
                  )
                  .toList();
        }

        final rawModelEvidence =
            response['model_forward_evidence'];

        if (rawModelEvidence is Map) {
          forwardStats =
              Map<String, dynamic>.from(
            rawModelEvidence,
          );
        }

        final rawIgPerformance =
            response['ig_demo_performance'];

        if (rawIgPerformance is Map) {
          igDemoPerformance =
              Map<String, dynamic>.from(
            rawIgPerformance,
          );
        }

        final rawLearning =
            response['learning'];

        if (rawLearning is Map) {
          aiLearningSnapshot =
              Map<String, dynamic>.from(
            rawLearning,
          );
        }

        final rawLearningWatchers =
            response['learning_watchers'];

        if (rawLearningWatchers is List) {
          aiLearningWatchers =
              rawLearningWatchers
                  .whereType<Map>()
                  .map(
                    (item) =>
                        Map<String, dynamic>.from(
                      item,
                    ),
                  )
                  .toList();
        }

        final rawV66Forward =
            response[
                'v66_forward_intelligence'];

        if (rawV66Forward is Map) {
          v66ForwardIntelligence =
              Map<String, dynamic>.from(
            rawV66Forward,
          );
        }
      });
    } catch (_) {
      // Auto dashboard is supplemental.
    }
  }

  void startAutoDashboardPolling() {
    autoDashboardPollTimer?.cancel();

    autoDashboardPollTimer =
        Timer.periodic(
      const Duration(
        seconds: 20,
      ),
      (_) {
        loadAutoDashboard();
        loadSystemOverview();
        loadAiLearningStatus();
        loadOvernightDemoStatus();
      },
    );

    Future.microtask(
      loadAutoDashboard,
    );

    Future.microtask(
      loadSystemOverview,
    );

    Future.microtask(
      loadAiLearningStatus,
    );

    Future.microtask(
      loadOvernightDemoStatus,
    );
  }

  Future<void> startAutoMode() async {
    if (autoManagerBusy) {
      return;
    }

    setState(() {
      autoManagerBusy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/auto-manager/start',
      ).replace(
        queryParameters: {
          'risk_mode': risk,
          'starting_balance':
              balance.text.trim(),
          'payout': '0.8',
          'scan_interval_minutes':
              '2',
          'target_active_watchers':
              '6',
          'scan_top_n': '9',
        },
      );

      final response = await http
          .post(uri)
          .timeout(
            const Duration(
              seconds: 45,
            ),
          );

      if (response.statusCode != 200) {
        throw Exception(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );
      }

      startAutoDashboardPolling();
      await loadAutoDashboard();
    } catch (e) {
      if (mounted) {
        setState(() {
          error =
              'Could not start Auto Mode: $e';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          autoManagerBusy = false;
        });
      } else {
        autoManagerBusy = false;
      }
    }
  }

  Future<void> stopAutoMode() async {
    if (autoManagerBusy) {
      return;
    }

    setState(() {
      autoManagerBusy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/auto-manager/stop',
      );

      final response = await http
          .post(uri)
          .timeout(
            const Duration(
              seconds: 45,
            ),
          );

      if (response.statusCode != 200) {
        throw Exception(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );
      }

      await loadAutoDashboard();
    } catch (e) {
      if (mounted) {
        setState(() {
          error =
              'Could not stop Auto Mode: $e';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          autoManagerBusy = false;
        });
      } else {
        autoManagerBusy = false;
      }
    }
  }

  Future<void> runAutoManagerNow() async {
    if (autoManagerBusy) {
      return;
    }

    setState(() {
      autoManagerBusy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/auto-manager/run-now',
      );

      final response = await http
          .post(uri)
          .timeout(
            const Duration(
              seconds: 45,
            ),
          );

      if (response.statusCode != 200) {
        throw Exception(
          'HTTP ${response.statusCode}: '
          '${response.body}',
        );
      }

      final decoded =
          jsonDecode(
        response.body,
      );

      if (decoded is Map &&
          mounted) {
        setState(() {
          autoManagerJob =
              Map<String, dynamic>.from(
            decoded,
          );
        });
      }

      startAutoDashboardPolling();
      await loadAutoDashboard();
    } catch (e) {
      if (mounted) {
        setState(() {
          error =
              'Auto Manager run failed: $e';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          autoManagerBusy = false;
        });
      } else {
        autoManagerBusy = false;
      }
    }
  }

  String formatEpochTime(
    dynamic value,
  ) {
    if (value == null) {
      return '-';
    }

    final seconds =
        double.tryParse(
      value.toString(),
    );

    if (seconds == null ||
        seconds <= 0) {
      return '-';
    }

    final dt = DateTime
        .fromMillisecondsSinceEpoch(
      (seconds * 1000).round(),
      isUtc: true,
    )
        .toLocal();

    return '${dt.hour.toString().padLeft(2, '0')}:'
        '${dt.minute.toString().padLeft(2, '0')}';
  }

  String watcherLifecycleSubtitle(
    Map<String, dynamic> watcher,
  ) {
    final status =
        watcher['status']
                ?.toString() ??
            '-';

    final health =
        watcher['strategy_health']
                ?.toString() ??
            'PROBATION';

    if (status == 'OPEN') {
      return '$health • Entry ${formatPrice(watcher['entry_price'])} • '
          'Exit target ${watcher['target_exit_at_iso'] ?? '-'}';
    }

    if (status == 'WIN' ||
        status == 'LOSS') {
      return '$health • P&L ${watcher['pnl'] ?? '-'} • '
          'Entry ${formatPrice(watcher['entry_price'])} → '
          '${formatPrice(watcher['exit_price'])}';
    }

    final reason =
        watcher['last_reason']
                ?.toString() ??
            'Awaiting next lifecycle update';

    return '$health • $reason';
  }

  // =========================================================
  // V5.3 SERVER VERIFIED WATCHER
  // =========================================================

  Future<void> createServerWatcher(
    Map<String, dynamic> verified,
  ) async {
    try {
      final uri = Uri.parse(
        '$apiBase/watchers',
      ).replace(
        queryParameters: {
          'risk_mode': risk,
          'starting_balance':
              balance.text.trim(),
          'payout': '0.8',
        },
      );

      final response =
          await postJsonOnce(
        uri,
        verified,
        timeoutSeconds: 60,
      );

      final rawWatcher =
          response['watcher'];

      if (rawWatcher is! Map) {
        throw const FormatException(
          'Watcher server did not return a watcher object',
        );
      }

      if (!mounted) {
        return;
      }

      final created =
          Map<String, dynamic>.from(
        rawWatcher,
      );

      setState(() {
        serverWatchers.removeWhere(
          (item) =>
              item['watcher_id'] ==
              created['watcher_id'],
        );

        serverWatchers.add(
          created,
        );

        serverWatcher =
            _selectPrimaryWatcher(
          serverWatchers,
        );
      });

      startWatcherPolling();
      await loadForwardStats();
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Verified setup passed, but server watcher could not start: $e';
      });
    }
  }

  void startWatcherPolling() {
    watcherPollTimer?.cancel();

    watcherPollTimer =
        Timer.periodic(
      const Duration(
        seconds: 20,
      ),
      (_) {
        refreshServerWatchers();
      },
    );

    Future.microtask(
      refreshServerWatchers,
    );
  }

  Map<String, dynamic>? _selectPrimaryWatcher(
    List<Map<String, dynamic>> watchers,
  ) {
    if (watchers.isEmpty) {
      return null;
    }

    int priority(
      Map<String, dynamic> item,
    ) {
      switch (
          item['status']
                  ?.toString() ??
              '') {
        case 'OPEN':
          return 100;
        case 'READY':
          return 90;
        case 'WATCHING':
          return 80;
        case 'RISK_BLOCKED':
          return 70;
        case 'WIN':
          return 60;
        case 'LOSS':
          return 50;
        case 'EXPIRED':
          return 30;
        case 'INVALIDATED':
          return 20;
        case 'SUPERSEDED':
          return 10;
        default:
          return 0;
      }
    }

    final sorted = [
      ...watchers,
    ];

    sorted.sort(
      (a, b) =>
          priority(b).compareTo(
        priority(a),
      ),
    );

    return sorted.first;
  }

  Future<void> refreshServerWatchers() async {
    if (watcherBusy) {
      return;
    }

    watcherBusy = true;

    try {
      final uri = Uri.parse(
        '$apiBase/watchers',
      );

      final response = await getJson(
        uri,
        timeoutSeconds: 45,
      );

      final raw =
          response['watchers'];

      if (raw is List &&
          mounted) {
        final updated = <
            Map<String, dynamic>>[];

        for (final item in raw) {
          if (item is Map) {
            updated.add(
              Map<String, dynamic>.from(
                item,
              ),
            );
          }
        }

        setState(() {
          serverWatchers = updated;

          serverWatcher =
              _selectPrimaryWatcher(
            updated,
          );
        });
      }

      await loadForwardStats();
    } catch (_) {
      // Watchers remain active on the server.
    } finally {
      watcherBusy = false;
    }
  }

  Future<void> refreshServerWatcher() async {
    await refreshServerWatchers();
  }

  Future<void> checkServerWatcherNow() async {
    if (watcherBusy ||
        serverWatcher == null) {
      return;
    }

    final watcherId =
        serverWatcher!['watcher_id']
            ?.toString();

    if (watcherId == null ||
        watcherId.isEmpty) {
      return;
    }

    setState(() {
      watcherBusy = true;
      error = null;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/watchers/$watcherId/check',
      );

      final response = await http
          .post(uri)
          .timeout(
            const Duration(
              seconds: 120,
            ),
          );

      if (response.statusCode != 200) {
        throw Exception(
          'HTTP ${response.statusCode}: ${response.body}',
        );
      }

      final decoded =
          jsonDecode(response.body);

      if (decoded is! Map ||
          decoded['watcher'] is! Map) {
        throw const FormatException(
          'Unexpected watcher response',
        );
      }

      if (!mounted) {
        return;
      }

      final checked =
          Map<String, dynamic>.from(
        decoded['watcher'] as Map,
      );

      setState(() {
        final index =
            serverWatchers.indexWhere(
          (item) =>
              item['watcher_id'] ==
              checked['watcher_id'],
        );

        if (index >= 0) {
          serverWatchers[index] =
              checked;
        } else {
          serverWatchers.add(
            checked,
          );
        }

        serverWatcher =
            _selectPrimaryWatcher(
          serverWatchers,
        );
      });

      await refreshServerWatchers();
    } catch (e) {
      if (mounted) {
        setState(() {
          error =
              'Watcher check failed: $e';
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          watcherBusy = false;
        });
      } else {
        watcherBusy = false;
      }
    }
  }

  Future<void> loadForwardStats() async {
    try {
      final uri = Uri.parse(
        '$apiBase/model-forward-evidence',
      ).replace(
        queryParameters: {
          'starting_balance':
              balance.text.trim(),
        },
      );

      final stats = await getJson(
        uri,
        timeoutSeconds: 45,
      );

      if (mounted) {
        setState(() {
          forwardStats = stats;
        });
      }
    } catch (_) {
      // Forward stats are supplemental. Do not interrupt
      // an active watcher when this request fails.
    }
  }

  Color watcherStatusColor(
    String status,
  ) {
    switch (status) {
      case 'OPEN':
      case 'WIN':
        return Colors.greenAccent;
      case 'WATCHING':
      case 'READY':
      case 'RISK_BLOCKED':
        return Colors.amberAccent;
      case 'LOSS':
      case 'EXPIRED':
      case 'INVALIDATED':
      case 'SUPERSEDED':
        return Colors.redAccent;
      default:
        return Colors.white70;
    }
  }

  IconData watcherStatusIcon(
    String status,
  ) {
    switch (status) {
      case 'OPEN':
        return Icons.play_circle_fill;
      case 'WIN':
        return Icons.emoji_events;
      case 'LOSS':
        return Icons.cancel;
      case 'WATCHING':
        return Icons.visibility;
      case 'RISK_BLOCKED':
        return Icons.shield;
      case 'EXPIRED':
        return Icons.timer_off;
      case 'INVALIDATED':
        return Icons.block;
      default:
        return Icons.sync;
    }
  }


  Color paperTradeColor(
    String status,
  ) {
    switch (status) {
      case 'WIN':
        return Colors.greenAccent;
      case 'LOSS':
        return Colors.redAccent;
      case 'OPEN':
        return Colors.lightBlueAccent;
      default:
        return Colors.white70;
    }
  }

  IconData paperTradeIcon(
    String status,
  ) {
    switch (status) {
      case 'WIN':
        return Icons.trending_up;
      case 'LOSS':
        return Icons.trending_down;
      case 'OPEN':
        return Icons.hourglass_top;
      default:
        return Icons.receipt_long;
    }
  }

  String paperTradeHeadline(
    String status,
  ) {
    switch (status) {
      case 'WIN':
        return 'WIN';
      case 'LOSS':
        return 'LOSS';
      case 'OPEN':
        return 'LIVE PAPER TRADE';
      default:
        return status;
    }
  }

  String formatMoney(
    dynamic value,
  ) {
    if (value == null) {
      return '-';
    }

    final number =
        value is num
            ? value.toDouble()
            : double.tryParse(
                  value.toString(),
                ) ??
                0.0;

    final sign =
        number > 0
            ? '+'
            : '';

    return '$sign${number.toStringAsFixed(2)}';
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

  double _profileMinConfidence() {
    switch (risk) {
      case 'Conservative':
        return 0.72;
      case 'Aggressive':
        return 0.62;
      case 'Balanced':
      default:
        return 0.67;
    }
  }

  int _intervalMinutes(
    dynamic interval,
  ) {
    final text =
        interval?.toString().trim().toLowerCase() ?? '';

    if (text.endsWith('m')) {
      return int.tryParse(
            text.substring(
              0,
              text.length - 1,
            ),
          ) ??
          15;
    }

    if (text.endsWith('h')) {
      final hours = int.tryParse(
            text.substring(
              0,
              text.length - 1,
            ),
          ) ??
          1;

      return hours * 60;
    }

    return 15;
  }

  Future<void>
      analyseVerifiedTrade() async {
    if (verifiedTrade == null ||
        busy) {
      return;
    }

    final selectedSymbol =
        verifiedTrade!['symbol']
            ?.toString();

    if (selectedSymbol == null ||
        selectedSymbol.isEmpty) {
      return;
    }

    setState(() {
      busy = true;
      error = null;
      liveEntryAssessment = null;
      symbol.text = selectedSymbol;
    });

    try {
      final uri = Uri.parse(
        '$apiBase/signal',
      ).replace(
        queryParameters: {
          'symbol':
              selectedSymbol,
          'risk_mode':
              risk,
          'balance':
              balance.text.trim(),
        },
      );

      final result = await getJson(
        uri,
        timeoutSeconds: 120,
      );

      final verifiedDirection =
          verifiedTrade!['direction']
                  ?.toString()
                  .toUpperCase() ??
              'WAIT';

      final liveDecision =
          result['decision']
                  ?.toString()
                  .toUpperCase() ??
              'WAIT';

      final confidence =
          ((result['confidence'] ?? 0)
                  as num)
              .toDouble();

      final aiUp =
          ((result[
                      'combined_up_probability'] ??
                  0.50)
              as num)
              .toDouble();

      final rsi =
          ((result['rsi'] ?? 50)
                  as num)
              .toDouble();

      final price =
          result['price'];

      final minConfidence =
          _profileMinConfidence();

      final reasons = <String>[];

      String entryStatus =
          'WAIT_CONFIRMATION';

      String headline =
          'WAIT FOR CONFIRMATION';

      // -----------------------------------------------------
      // 1. Opposite live signal with meaningful confidence
      // -----------------------------------------------------

      final oppositeSignal =
          (verifiedDirection == 'BUY' &&
                  liveDecision == 'SELL') ||
              (verifiedDirection == 'SELL' &&
                  liveDecision == 'BUY');

      if (oppositeSignal &&
          confidence >=
              minConfidence) {
        entryStatus =
            'SETUP_INVALIDATED';

        headline =
            'SETUP INVALIDATED';

        reasons.add(
          'The live AI signal now points '
          'against the verified historical direction.',
        );
      }

      // -----------------------------------------------------
      // 2. Overextended RSI: wait for pullback
      // -----------------------------------------------------

      else if (
          verifiedDirection == 'BUY' &&
          rsi >= 70.0) {
        entryStatus =
            'WAIT_PULLBACK';

        headline =
            'WAIT FOR PULLBACK';

        reasons.add(
          'BUY setup is historically verified, '
          'but RSI is overextended at '
          '${rsi.toStringAsFixed(1)}.',
        );
      } else if (
          verifiedDirection == 'SELL' &&
          rsi <= 30.0) {
        entryStatus =
            'WAIT_PULLBACK';

        headline =
            'WAIT FOR PULLBACK';

        reasons.add(
          'SELL setup is historically verified, '
          'but RSI is oversold at '
          '${rsi.toStringAsFixed(1)}.',
        );
      }

      // -----------------------------------------------------
      // 3. Live direction + confidence + AI probability agree
      // -----------------------------------------------------

      else {
        final directionMatches =
            liveDecision ==
                verifiedDirection;

        final confidencePass =
            confidence >=
                minConfidence;

        final probabilityPass =
            verifiedDirection == 'BUY'
                ? aiUp >= 0.60
                : aiUp <= 0.40;

        if (directionMatches &&
            confidencePass &&
            probabilityPass) {
          entryStatus =
              'ENTER_NOW';

          headline =
              'ENTRY CONFIRMED';

          reasons.add(
            'Historical validation and the '
            'current live AI signal agree.',
          );

          reasons.add(
            'Live confidence passed the '
            '${(minConfidence * 100).toStringAsFixed(0)}% '
            '$risk threshold.',
          );
        } else {
          entryStatus =
              'WAIT_CONFIRMATION';

          headline =
              'WAIT FOR CONFIRMATION';

          if (!directionMatches) {
            reasons.add(
              'The live signal is $liveDecision '
              'while the verified setup is '
              '$verifiedDirection.',
            );
          }

          if (!confidencePass) {
            reasons.add(
              'Live confidence '
              '${(confidence * 100).toStringAsFixed(1)}% '
              'is below the '
              '${(minConfidence * 100).toStringAsFixed(0)}% '
              '$risk threshold.',
            );
          }

          if (!probabilityPass) {
            reasons.add(
              'The live AI probability has not '
              'confirmed the verified direction strongly enough.',
            );
          }
        }
      }

      final intervalMinutes =
          _intervalMinutes(
        verifiedTrade!['interval'],
      );

      final holdingCandles =
          int.tryParse(
                '${verifiedTrade!['holding_candles'] ?? 0}',
              ) ??
              0;

      final historicalHoldingMinutes =
          intervalMinutes *
              holdingCandles;

      if (!mounted) {
        return;
      }

      setState(() {
        sig = result;

        liveEntryAssessment = {
          'status':
              entryStatus,

          'headline':
              headline,

          'verified_direction':
              verifiedDirection,

          'live_decision':
              liveDecision,

          'confidence':
              confidence,

          'ai_up_probability':
              aiUp,

          'rsi':
              rsi,

          'price':
              price,

          'signal_reason':
              result['reason'],

          'reasons':
              reasons,

          'interval_minutes':
              intervalMinutes,

          'recheck_minutes':
              intervalMinutes,

          'historical_holding_minutes':
              historicalHoldingMinutes,
        };
      });
    } catch (e) {
      if (!mounted) {
        return;
      }

      setState(() {
        error =
            'Live entry confirmation failed: $e';
      });
    } finally {
      if (mounted) {
        setState(() {
          busy = false;
        });
      }
    }
  }

  Color liveEntryColor(
    String status,
  ) {
    switch (status) {
      case 'ENTER_NOW':
        return Colors.greenAccent;

      case 'WAIT_PULLBACK':
        return Colors.amberAccent;

      case 'WAIT_CONFIRMATION':
        return Colors.amberAccent;

      case 'SETUP_INVALIDATED':
        return Colors.redAccent;

      default:
        return Colors.white70;
    }
  }

  IconData liveEntryIcon(
    String status,
  ) {
    switch (status) {
      case 'ENTER_NOW':
        return Icons.play_circle_fill;

      case 'WAIT_PULLBACK':
        return Icons.trending_down;

      case 'WAIT_CONFIRMATION':
        return Icons.hourglass_top;

      case 'SETUP_INVALIDATED':
        return Icons.block;

      default:
        return Icons.info_outline;
    }
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
    WidgetsBinding.instance.addObserver(this);

    Future.microtask(
      refreshSignal,
    );

    Future.microtask(
      loadForwardStats,
    );

    Future.microtask(
      loadAutoDashboard,
    );

    startAutoDashboardPolling();
  }

  @override
  void didChangeAppLifecycleState(
    AppLifecycleState state,
  ) {
    if (state == AppLifecycleState.resumed) {
      // The backend/IG connection is authoritative. Whenever Android resumes
      // the UI, immediately rebuild the phone view from server + IG truth.
      startAutoDashboardPolling();
      Future.microtask(loadOvernightDemoStatus);
      Future.microtask(loadAutoDashboard);
      Future.microtask(loadAiLearningStatus);
      Future.microtask(loadSystemOverview);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    watcherPollTimer?.cancel();
    autoDashboardPollTimer?.cancel();
    symbol.dispose();
    balance.dispose();
    copilotController.dispose();

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

                  Column(
                    crossAxisAlignment:
                        CrossAxisAlignment.end,
                    children: [
                      Text(
                        'Smart Score '
                        '${score.toStringAsFixed(1)}/100',
                        style:
                            const TextStyle(
                          fontWeight:
                              FontWeight.bold,
                        ),
                      ),
                      if (market[
                              'quality_tier'] !=
                          null)
                        Text(
                          'Tier '
                          '${market['quality_tier']}'
                          '${market['raw_fast_score'] != null ? ' • Raw ${formatNumber(market['raw_fast_score'], decimals: 1)}' : ''}',
                          style:
                              const TextStyle(
                            fontSize: 11,
                            color:
                                Colors.white70,
                          ),
                        ),
                    ],
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
    final cs = Theme.of(context).colorScheme;

    final liveDecision =
        sig?['decision']?.toString().toUpperCase() ?? 'WAIT';
    final confidence = formatPercent(sig?['confidence']);
    final aiUp = formatPercent(sig?['combined_up_probability']);

    final dashboard = autoDashboard ?? <String, dynamic>{};
    final manager = dashboard['manager'] is Map
        ? Map<String, dynamic>.from(dashboard['manager'] as Map)
        : <String, dynamic>{};
    final summary = dashboard['summary'] is Map
        ? Map<String, dynamic>.from(dashboard['summary'] as Map)
        : <String, dynamic>{};

    final autoOn = dashboard['auto_mode'] == true ||
        manager['enabled'] == true ||
        systemOverview?['auto_manager_enabled'] == true;

    final stage = summary['current_stage']?.toString() ??
        manager['progress_stage']?.toString() ??
        'IDLE';
    final stageMessage = summary['current_message']?.toString() ??
        manager['progress_message']?.toString() ??
        'Waiting for next automatic cycle';
    final progress = double.tryParse(
          '${summary['progress_percent'] ?? manager['progress_percent'] ?? 0}',
        ) ??
        0.0;

    final activeWatchers = int.tryParse(
          '${summary['active_watchers'] ?? serverWatchers.length}',
        ) ??
        serverWatchers.length;
    final targetWatchers = int.tryParse(
          '${summary['target_active_watchers'] ?? 6}',
        ) ??
        6;
    final openTrades = int.tryParse(
          '${summary['open_trades'] ?? paperTrades.where((t) => t['status'] == 'OPEN').length}',
        ) ??
        0;

    final forwardTrades = forwardStats?['forward_trades'] ??
        forwardStats?['trades'] ??
        0;
    final paperBalance = forwardStats?['paper_balance'] ?? balance.text.trim();
    final totalPnl = forwardStats?['total_pnl'] ??
        forwardStats?['pnl'] ??
        0;
    final forwardWr = forwardStats?['win_rate_pct'] ??
        forwardStats?['forward_win_rate_pct'] ??
        0;
    final modelOpen =
        forwardStats?['open_entries'] ?? 0;
    final modelSettled =
        forwardStats?['settled_entries'] ?? 0;
    final modelWins =
        forwardStats?['wins'] ?? 0;
    final modelLosses =
        forwardStats?['losses'] ?? 0;
    final modelBrokerMatched =
        forwardStats?['broker_matched_entries'] ?? 0;
    final recoveredUnattributed =
        forwardStats?['broker_recovered_unattributed'] ?? 0;

    final brokerPerf =
        igDemoPerformance ??
            <String, dynamic>{};

    final igAccepted =
        brokerPerf['accepted_trades'] ?? 0;
    final igOpen =
        brokerPerf['open_positions'] ?? 0;
    final igClosed =
        brokerPerf['closed_positions'] ?? 0;
    final igGraded =
        brokerPerf['graded_trades'] ??
            brokerPerf['trades'] ??
            0;
    final igWins =
        brokerPerf['wins'] ?? 0;
    final igLosses =
        brokerPerf['losses'] ?? 0;
    final igWinRate =
        brokerPerf['win_rate_pct'] ?? 0;
    final igBalance =
        brokerPerf['account_balance'];
    final igAvailable =
        brokerPerf['account_available'];
    final igRunningPnl =
        brokerPerf['account_profit_loss'];
    final igCurrency =
        brokerPerf['account_currency']
                ?.toString() ??
            '';

    final topCandidates = (fastScan?['top_candidates'] as List?) ?? const [];
    final ranking = (fastScan?['ranking'] as List?) ?? const [];

    Color sideColor(String value) {
      final v = value.toUpperCase();
      if (v == 'BUY' || v == 'WIN' || v == 'OPEN') {
        return const Color(0xFF67F0C1);
      }
      if (v == 'SELL' || v == 'LOSS') {
        return const Color(0xFFFF6B75);
      }
      return const Color(0xFFFFD75E);
    }

    Widget glassCard({
      required Widget child,
      EdgeInsets padding = const EdgeInsets.all(16),
      Color? glow,
    }) {
      return Container(
        padding: padding,
        decoration: BoxDecoration(
          color: const Color(0xFF0E1A24).withValues(alpha: .94),
          borderRadius: BorderRadius.circular(22),
          border: Border.all(
            color: glow?.withValues(alpha: .28) ?? const Color(0xFF18313C),
          ),
          boxShadow: [
            BoxShadow(
              color: (glow ?? Colors.black).withValues(alpha: .08),
              blurRadius: 26,
              offset: const Offset(0, 12),
            ),
          ],
        ),
        child: child,
      );
    }

    Widget sectionTitle(String title, {String? subtitle}) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      fontSize: 17,
                      fontWeight: FontWeight.w900,
                      letterSpacing: .2,
                    ),
                  ),
                  if (subtitle != null) ...[
                    const SizedBox(height: 3),
                    Text(
                      subtitle,
                      style: const TextStyle(
                        fontSize: 11,
                        color: Colors.white54,
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      );
    }

    Widget statTile(
      String label,
      String value,
      IconData icon, {
      Color? valueColor,
    }) {
      return Expanded(
        child: glassCard(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(icon, color: cs.primary, size: 20),
              const SizedBox(height: 14),
              Text(
                value,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 19,
                  fontWeight: FontWeight.w900,
                  color: valueColor ?? Colors.white,
                ),
              ),
              const SizedBox(height: 2),
              Text(
                label,
                style: const TextStyle(
                  color: Colors.white54,
                  fontSize: 11,
                ),
              ),
            ],
          ),
        ),
      );
    }

    Widget pill(
      String text, {
      IconData? icon,
      Color? color,
    }) {
      final c = color ?? cs.primary;
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
        decoration: BoxDecoration(
          color: c.withValues(alpha: .12),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: c.withValues(alpha: .20)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(icon, size: 13, color: c),
              const SizedBox(width: 5),
            ],
            Text(
              text,
              style: TextStyle(
                fontSize: 10,
                fontWeight: FontWeight.w900,
                color: c,
                letterSpacing: .25,
              ),
            ),
          ],
        ),
      );
    }

    Widget watcherCard(Map<String, dynamic> w) {
      final market = w['market']?.toString() ?? w['symbol']?.toString() ?? 'MARKET';
      final direction = w['direction']?.toString().toUpperCase() ?? 'WAIT';
      final status = w['status']?.toString().toUpperCase() ?? 'WATCHING';
      final conf = formatPercent(w['confidence']);
      final reason = w['last_reason']?.toString() ?? watcherLifecycleSubtitle(w);
      return glassCard(
        glow: sideColor(direction),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                color: sideColor(direction).withValues(alpha: .12),
                borderRadius: BorderRadius.circular(15),
              ),
              child: Icon(Icons.currency_exchange_rounded, color: sideColor(direction)),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          market,
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w900,
                          ),
                        ),
                      ),
                      Text(
                        direction,
                        style: TextStyle(
                          color: sideColor(direction),
                          fontWeight: FontWeight.w900,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 5),
                  Row(
                    children: [
                      pill(status, color: watcherStatusColor(status)),
                      const SizedBox(width: 7),
                      if (conf != '0.0')
                        Text(
                          '$conf%',
                          style: const TextStyle(
                            color: Colors.white60,
                            fontSize: 11,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    reason,
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white60,
                      fontSize: 12,
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

    Widget dashboardPage() {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 120),
        children: [
          Container(
            padding: const EdgeInsets.all(18),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(26),
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  cs.primary.withValues(alpha: .22),
                  const Color(0xFF0D2630),
                  const Color(0xFF0B1620),
                ],
              ),
              border: Border.all(color: cs.primary.withValues(alpha: .25)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    pill(
                      autoOn ? 'AUTO MANAGER ON' : 'AUTO MANAGER OFF',
                      icon: autoOn ? Icons.bolt_rounded : Icons.pause_rounded,
                      color: autoOn ? cs.primary : const Color(0xFFFFD75E),
                    ),
                    const Spacer(),
                    Container(
                      width: 8,
                      height: 8,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: systemOverview != null || autoDashboard != null
                            ? const Color(0xFF67F0C1)
                            : const Color(0xFFFFD75E),
                      ),
                    ),
                    const SizedBox(width: 6),
                    const Text(
                      'V6.5',
                      style: TextStyle(
                        fontSize: 11,
                        fontWeight: FontWeight.w900,
                        color: Colors.white60,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 24),
                Text(
                  stage.replaceAll('_', ' '),
                  style: const TextStyle(
                    fontSize: 27,
                    fontWeight: FontWeight.w900,
                    letterSpacing: .2,
                  ),
                ),
                const SizedBox(height: 5),
                Text(
                  stageMessage,
                  style: const TextStyle(
                    color: Colors.white60,
                    fontSize: 13,
                    height: 1.35,
                  ),
                ),
                const SizedBox(height: 18),
                ClipRRect(
                  borderRadius: BorderRadius.circular(99),
                  child: LinearProgressIndicator(
                    value: progress.clamp(0, 100) / 100,
                    minHeight: 7,
                    backgroundColor: Colors.white10,
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Text(
                      'Next scan ${formatEpochTime(summary['next_scan_at'])}',
                      style: const TextStyle(color: Colors.white54, fontSize: 11),
                    ),
                    const Spacer(),
                    Text(
                      '${progress.round()}%',
                      style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 11),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              statTile(
                igBalance != null ? 'IG funds' : 'Paper balance',
                igBalance != null
                    ? '${igCurrency.isNotEmpty ? '$igCurrency ' : ''}${formatMoney(igBalance)}'
                    : '$paperBalance',
                Icons.account_balance_wallet_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'IG positions',
                '$igOpen',
                Icons.swap_horiz_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile('Watchers', '$activeWatchers / $targetWatchers', Icons.visibility_outlined),
              const SizedBox(width: 10),
              statTile('Phase', '$igAccepted / 10', Icons.flag_outlined),
            ],
          ),
          const SizedBox(height: 20),
          sectionTitle('Live intelligence', subtitle: 'Current signal for ${symbol.text.trim()}'),
          glassCard(
            glow: sideColor(liveDecision),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Text(
                      liveDecision,
                      style: TextStyle(
                        fontSize: 34,
                        fontWeight: FontWeight.w900,
                        color: sideColor(liveDecision),
                      ),
                    ),
                    const Spacer(),
                    pill(risk.toUpperCase(), icon: Icons.shield_outlined),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  sig?['reason']?.toString() ?? 'Waiting for signal...',
                  style: const TextStyle(color: Colors.white60, height: 1.35),
                ),
                const SizedBox(height: 16),
                Row(
                  children: [
                    Expanded(child: _midnightValue('Quant', '$confidence%')),
                    const SizedBox(width: 8),
                    Expanded(child: _midnightValue('AI up', '$aiUp%')),
                    const SizedBox(width: 8),
                    Expanded(child: _midnightValue('RSI', formatNumber(sig?['rsi']))),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: busy ? null : scanAllMarkets,
                  icon: const Icon(Icons.radar_rounded),
                  label: const Text('Scan markets'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton.tonalIcon(
                  onPressed: busy ? null : findVerifiedTrade,
                  icon: const Icon(Icons.verified_rounded),
                  label: const Text('Find setup'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 20),
          sectionTitle('Active watchers', subtitle: 'Verified setups under live observation'),
          if (serverWatchers.isEmpty)
            glassCard(
              child: const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: Row(
                  children: [
                    Icon(Icons.visibility_off_outlined, color: Colors.white38),
                    SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        'No watcher snapshot loaded yet. Auto Manager will populate this automatically.',
                        style: TextStyle(color: Colors.white54),
                      ),
                    ),
                  ],
                ),
              ),
            )
          else
            ...serverWatchers.take(4).map((w) => Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: watcherCard(w),
                )),
          const SizedBox(height: 10),
          sectionTitle('V6.5 learning policy'),
          glassCard(
            child: const Column(
              children: [
                _MidnightRuleRow('Normal PAPER path', '≥ 30%', 'Verified + live direction agrees'),
                Divider(height: 24),
                _MidnightRuleRow('AI PAPER path', '≥ 40%', 'AI approves + direction agrees'),
                Divider(height: 24),
                _MidnightRuleRow('Legacy 67% gate', 'OFF', 'Shadow-risk learning remains active'),
              ],
            ),
          ),
          if (error != null) ...[
            const SizedBox(height: 14),
            glassCard(
              glow: const Color(0xFFFF6B75),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Icon(Icons.error_outline_rounded, color: Color(0xFFFF6B75)),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      error!,
                      style: const TextStyle(color: Color(0xFFFF9098), fontSize: 12),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      );
    }

    Widget marketsPage() {
      return ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 120),
        children: [
          sectionTitle('Market scanner', subtitle: 'Fast ranking across the configured FX universe'),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: busy ? null : scanAllMarkets,
                  icon: scanningMarkets
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.radar_rounded),
                  label: Text(scanningMarkets ? 'Scanning...' : 'Fast scan'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton.tonalIcon(
                  onPressed: busy ? null : findVerifiedTrade,
                  icon: const Icon(Icons.verified_rounded),
                  label: const Text('Deep verify'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          if (topCandidates.isEmpty)
            glassCard(
              child: const Text(
                'Run a market scan to populate ranked opportunities.',
                style: TextStyle(color: Colors.white54),
              ),
            )
          else ...[
            sectionTitle('Top opportunities'),
            ...topCandidates.take(6).whereType<Map>().map((raw) {
              final item = Map<String, dynamic>.from(raw);
              final market = item['market']?.toString() ?? item['symbol']?.toString() ?? '-';
              final direction = item['direction']?.toString().toUpperCase() ?? 'WAIT';
              final score = item['smart_fast_score'] ?? item['fast_score'] ?? item['score'] ?? '-';
              return Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: InkWell(
                  borderRadius: BorderRadius.circular(22),
                  onTap: () => analyseMarket(item),
                  child: glassCard(
                    glow: sideColor(direction),
                    child: Row(
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(market, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w900)),
                              const SizedBox(height: 4),
                              Text(
                                item['reason']?.toString() ?? item['status']?.toString() ?? 'Ranked opportunity',
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(color: Colors.white54, fontSize: 12),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(width: 10),
                        Column(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Text(direction, style: TextStyle(color: sideColor(direction), fontWeight: FontWeight.w900)),
                            const SizedBox(height: 4),
                            Text('$score', style: const TextStyle(color: Colors.white60, fontSize: 12)),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
              );
            }),
          ],
          if (ranking.isNotEmpty) ...[
            const SizedBox(height: 10),
            sectionTitle('Full ranking'),
            glassCard(
              child: Column(
                children: ranking.take(20).whereType<Map>().toList().asMap().entries.map((entry) {
                  final item = Map<String, dynamic>.from(entry.value);
                  final market = item['market']?.toString() ?? item['symbol']?.toString() ?? '-';
                  final direction = item['direction']?.toString().toUpperCase() ?? 'WAIT';
                  final score = item['smart_fast_score'] ?? item['score'] ?? item['fast_score'] ?? '-';
                  return Column(
                    children: [
                      if (entry.key > 0) const Divider(height: 18),
                      Row(
                        children: [
                          SizedBox(width: 30, child: Text('#${entry.key + 1}', style: const TextStyle(color: Colors.white38))),
                          Expanded(child: Text(market, style: const TextStyle(fontWeight: FontWeight.w800))),
                          Text(direction, style: TextStyle(color: sideColor(direction), fontWeight: FontWeight.w800)),
                          const SizedBox(width: 12),
                          SizedBox(width: 48, child: Text('$score', textAlign: TextAlign.right, style: const TextStyle(color: Colors.white60))),
                        ],
                      ),
                    ],
                  );
                }).toList(),
              ),
            ),
          ],
          if (validationHistory.isNotEmpty) ...[
            const SizedBox(height: 20),
            sectionTitle('Deep validation history'),
            ...validationHistory.take(8).map((item) {
              final ok = item['verified'] == true;
              return Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: glassCard(
                  glow: ok ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E),
                  child: Row(
                    children: [
                      Icon(ok ? Icons.verified_rounded : Icons.manage_search_rounded, color: ok ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(item['market']?.toString() ?? '-', style: const TextStyle(fontWeight: FontWeight.w900)),
                            const SizedBox(height: 3),
                            Text(item['deep_status']?.toString() ?? '-', style: const TextStyle(color: Colors.white54, fontSize: 11)),
                          ],
                        ),
                      ),
                      if (item['win_rate'] != null)
                        Text('${formatPercent(item['win_rate'])}%', style: const TextStyle(fontWeight: FontWeight.w800)),
                    ],
                  ),
                ),
              );
            }),
          ],
        ],
      );
    }

    Widget tradesPage() {
      final pnlValue =
          double.tryParse(
            '${igRunningPnl ?? 0}',
          ) ??
          0.0;

      return ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 120),
        children: [
          sectionTitle(
            'IG DEMO performance',
            subtitle:
                'Broker-grounded Phase-1 performance • IG is the source of truth',
          ),
          Row(
            children: [
              statTile(
                'Phase trades',
                '$igAccepted / 10',
                Icons.flag_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'Open IG',
                '$igOpen',
                Icons.swap_horiz_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'Settled IG',
                '$igClosed',
                Icons.fact_check_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'IG W / L',
                '$igWins / $igLosses',
                Icons.insights_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'IG win rate',
                '${formatNumber(igWinRate, decimals: 1)}%',
                Icons.percent_rounded,
                valueColor:
                    (double.tryParse('$igWinRate') ?? 0) > 0
                        ? const Color(0xFF67F0C1)
                        : Colors.white,
              ),
              const SizedBox(width: 10),
              statTile(
                'Running P&L',
                igRunningPnl == null
                    ? '-'
                    : '${igCurrency.isNotEmpty ? '$igCurrency ' : ''}${formatMoney(igRunningPnl)}',
                Icons.payments_outlined,
                valueColor:
                    pnlValue >= 0
                        ? const Color(0xFF67F0C1)
                        : const Color(0xFFFF6B75),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'IG funds',
                igBalance == null
                    ? '-'
                    : '${igCurrency.isNotEmpty ? '$igCurrency ' : ''}${formatMoney(igBalance)}',
                Icons.account_balance_wallet_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'Available',
                igAvailable == null
                    ? '-'
                    : '${igCurrency.isNotEmpty ? '$igCurrency ' : ''}${formatMoney(igAvailable)}',
                Icons.savings_outlined,
              ),
            ],
          ),
          const SizedBox(height: 8),
          glassCard(
            child: Text(
              igGraded == 0
                  ? 'Performance grading starts as IG positions close. Open positions still contribute to the broker running P&L above.'
                  : '$igGraded settled broker trade(s) currently have a WIN/LOSS outcome.',
              style: const TextStyle(
                color: Colors.white54,
                fontSize: 11,
              ),
            ),
          ),
          const SizedBox(height: 20),
          sectionTitle(
            'Trade journal',
            subtitle:
                'Internal AI entries + broker-reconciled IG DEMO positions',
          ),
          if (paperTrades.isEmpty)
            glassCard(
              child: const Column(
                children: [
                  Icon(
                    Icons.hourglass_empty_rounded,
                    size: 38,
                    color: Colors.white30,
                  ),
                  SizedBox(height: 10),
                  Text(
                    'No reconciled trades loaded yet.',
                    style: TextStyle(
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  SizedBox(height: 4),
                  Text(
                    'IG DEMO positions and PAPER learning trades will reappear here after server reconciliation.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Colors.white54,
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
            )
          else
            ...paperTrades.take(30).map(
              (trade) {
                final status =
                    trade['status']
                            ?.toString()
                            .toUpperCase() ??
                        '-';
                final market =
                    trade['market']?.toString() ??
                        trade['symbol']?.toString() ??
                        '-';
                final direction =
                    trade['direction']
                            ?.toString()
                            .toUpperCase() ??
                        '-';
                final broker =
                    trade['broker']?.toString() ??
                        '';
                final entryPath =
                    trade['entry_class'] ??
                        trade['entry_path'] ??
                        '-';
                final pnl = trade['pnl'];

                return Padding(
                  padding:
                      const EdgeInsets.only(
                    bottom: 10,
                  ),
                  child: glassCard(
                    glow:
                        paperTradeColor(status),
                    child: Row(
                      children: [
                        Icon(
                          paperTradeIcon(status),
                          color:
                              paperTradeColor(status),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment:
                                CrossAxisAlignment.start,
                            children: [
                              Text(
                                '$market  $direction',
                                style:
                                    const TextStyle(
                                  fontWeight:
                                      FontWeight.w900,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                '${paperTradeHeadline(status)} • $entryPath'
                                '${broker.isNotEmpty ? ' • $broker DEMO' : ''}',
                                style:
                                    const TextStyle(
                                  color:
                                      Colors.white54,
                                  fontSize: 11,
                                ),
                              ),
                              if (trade['entry_price'] != null) ...[
                                const SizedBox(height: 3),
                                Text(
                                  'Entry ${formatNumber(trade['entry_price'], decimals: 5)}'
                                  '${trade['ig_size'] != null ? ' • Size ${trade['ig_size']}' : ''}',
                                  style:
                                      const TextStyle(
                                    color:
                                        Colors.white38,
                                    fontSize: 10,
                                  ),
                                ),
                              ],
                            ],
                          ),
                        ),
                        Text(
                          pnl == null
                              ? (status == 'OPEN'
                                  ? 'OPEN'
                                  : '-')
                              : formatMoney(pnl),
                          style: TextStyle(
                            color:
                                paperTradeColor(status),
                            fontWeight:
                                FontWeight.w900,
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          const SizedBox(height: 20),
          sectionTitle(
            'Model forward evidence',
            subtitle:
                'V6 AI-learning entries • open trades count immediately; W/L only after settlement',
          ),
          Row(
            children: [
              statTile(
                'Model entries',
                '$forwardTrades',
                Icons.receipt_long_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'Open model',
                '$modelOpen',
                Icons.hourglass_top_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'Settled model',
                '$modelSettled',
                Icons.fact_check_outlined,
              ),
              const SizedBox(width: 10),
              statTile(
                'Model W / L',
                '$modelWins / $modelLosses',
                Icons.insights_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'Model WR',
                '${formatNumber(forwardWr, decimals: 1)}%',
                Icons.percent_rounded,
              ),
              const SizedBox(width: 10),
              statTile(
                'Broker matched',
                '$modelBrokerMatched',
                Icons.link_rounded,
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              statTile(
                'Model P&L',
                formatMoney(totalPnl),
                Icons.analytics_outlined,
                valueColor:
                    (double.tryParse('$totalPnl') ?? 0) >= 0
                        ? const Color(0xFF67F0C1)
                        : const Color(0xFFFF6B75),
              ),
              const SizedBox(width: 10),
              statTile(
                'Paper balance',
                '$paperBalance',
                Icons.account_balance_wallet_outlined,
              ),
            ],
          ),
          if ((int.tryParse('$recoveredUnattributed') ?? 0) > 0) ...[
            const SizedBox(height: 8),
            glassCard(
              child: Text(
                '$recoveredUnattributed IG-recovered position(s) are preserved as broker evidence but are not falsely attributed to the model because their original confidence metadata was lost before Always-Sync persistence.',
                style: const TextStyle(
                  color: Colors.amberAccent,
                  fontSize: 11,
                  height: 1.35,
                ),
              ),
            ),
          ],
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: () async {
                await loadAutoDashboard();
                await loadOvernightDemoStatus();
                await loadForwardStats();
              },
              icon: const Icon(
                Icons.refresh_rounded,
              ),
              label: const Text(
                'Refresh performance',
              ),
            ),
          ),
        ],
      );
    }

    Widget aiPage() {
      final learning =
          aiLearningSnapshot ??
              <String, dynamic>{};

      final mode =
          aiLearningStatus?['mode']
                  ?.toString() ??
              'DIRECT_AI40_SHADOW_PROMOTION_V662';

      final aiFloor =
          aiLearningStatus?[
                  'ai_min_confidence_pct'] ??
              40.0;

      final engineEnabled =
          learning['enabled'] == true;

      final liveExecution =
          learning['live_execution'] == true ||
              aiLearningStatus?[
                      'broker_execution_enabled'] ==
                  true;

      final activeWatchers =
          learning['active_watchers'] ?? 0;

      final openTrades =
          learning['open_trades'] ?? 0;

      final learningBalance =
          learning['paper_balance'] ?? 10000;

      final overnight =
          overnightDemoStatus ??
              <String, dynamic>{};

      final overnightSummary =
          overnight['summary'] is Map
              ? Map<String, dynamic>.from(
                  overnight['summary'],
                )
              : <String, dynamic>{};

      final overnightState =
          overnight['status']
                  ?.toString()
                  .toUpperCase() ??
              'PAUSED';

      final overnightActive =
          overnightState == 'ACTIVE';

      final overnightDraining =
          overnightState == 'DRAINING';

      final overnightComplete =
          overnightState ==
              'PHASE_COMPLETE';

      final phaseAccepted =
          overnightSummary[
                  'phase_accepted_trades'] ??
              0;

      final phaseTarget =
          overnightSummary['phase_target'] ??
              10;

      final phaseRemaining =
          overnightSummary[
                  'phase_remaining'] ??
              phaseTarget;

      final brokerPositions =
          overnightSummary[
                  'open_broker_positions'] ??
              0;

      final maxBrokerPositions =
          overnightSummary[
                  'max_broker_positions'] ??
              3;

      final overnightIg =
          overnight['ig_demo'] is Map
              ? Map<String, dynamic>.from(
                  overnight['ig_demo'],
                )
              : <String, dynamic>{};

      final brokerSyncState =
          overnightSummary[
                  'broker_sync_state']
                  ?.toString()
                  .toUpperCase() ??
              overnightIg['sync_state']
                  ?.toString()
                  .toUpperCase() ??
              'STALE';

      final brokerSyncAge =
          overnightSummary[
                  'broker_sync_age_seconds'] ??
              overnightIg[
                  'broker_sync_age_seconds'];

      final brokerPositionRows =
          overnight['broker_positions'] is List
              ? (overnight[
                          'broker_positions']
                      as List)
                  .whereType<Map>()
                  .map(
                    (item) =>
                        Map<String, dynamic>.from(
                      item,
                    ),
                  )
                  .toList()
              : <Map<String, dynamic>>[];

      final overnightWins =
          overnightSummary['wins'] ?? 0;

      final overnightLosses =
          overnightSummary['losses'] ?? 0;

      final overnightWinRate =
          overnightSummary[
                  'win_rate_pct'] ??
              0.0;

      final aiTrades = paperTrades
          .where(
            (trade) =>
                trade['source']?.toString() ==
                    'V66_LEARNING_ENGINE',
          )
          .toList();

      Map<String, dynamic>? openAiTrade;

      for (final trade in aiTrades) {
        if (trade['status']
                ?.toString()
                .toUpperCase() ==
            'OPEN') {
          openAiTrade = trade;
          break;
        }
      }

      Widget learningTradeCard(
        Map<String, dynamic> trade,
      ) {
        final status =
            trade['status']
                    ?.toString()
                    .toUpperCase() ??
                '-';

        final market =
            trade['market']?.toString() ??
                trade['symbol']?.toString() ??
                '-';

        final direction =
            trade['direction']
                    ?.toString()
                    .toUpperCase() ??
                '-';

        final entryClass =
            trade['entry_path']?.toString() ??
                trade['entry_class']?.toString() ??
                '-';

        final aiConfidence =
            trade['model_ai_confidence'];

        final quant =
            trade['entry_confidence'] ??
                trade['quant_confidence'];

        final dueAt =
            trade['settlement_due_at'] ??
                trade['scheduled_close_at'];

        return glassCard(
          glow: paperTradeColor(status),
          child: Column(
            crossAxisAlignment:
                CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    paperTradeIcon(status),
                    color:
                        paperTradeColor(status),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      '$market  $direction',
                      style: const TextStyle(
                        fontSize: 17,
                        fontWeight:
                            FontWeight.w900,
                      ),
                    ),
                  ),
                  pill(
                    entryClass,
                    color: const Color(
                      0xFF65E6D3,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              Row(
                children: [
                  statTile(
                    'Model AI',
                    aiConfidence is num
                        ? '${formatPercent(aiConfidence)}%'
                        : '-',
                    Icons.psychology_alt_rounded,
                    valueColor:
                        Colors.greenAccent,
                  ),
                  const SizedBox(width: 10),
                  statTile(
                    'Quant',
                    quant is num
                        ? '${formatPercent(quant)}%'
                        : '-',
                    Icons.analytics_outlined,
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Row(
                children: [
                  statTile(
                    'Stake',
                    formatMoney(
                      trade['stake'],
                    ),
                    Icons.payments_outlined,
                  ),
                  const SizedBox(width: 10),
                  statTile(
                    status == 'OPEN'
                        ? 'Time left'
                        : 'P&L',
                    status == 'OPEN'
                        ? formatEpochCountdown(
                            dueAt,
                          )
                        : formatMoney(
                            trade['pnl'],
                          ),
                    status == 'OPEN'
                        ? Icons.timer_outlined
                        : Icons
                            .account_balance_wallet_outlined,
                    valueColor:
                        status == 'WIN'
                            ? Colors.greenAccent
                            : status == 'LOSS'
                                ? Colors.redAccent
                                : Colors.white,
                  ),
                ],
              ),
              const SizedBox(height: 12),
              Text(
                'Entry ${formatNumber(trade['entry_price'], decimals: 5)}'
                '${trade['exit_price'] != null ? '  •  Exit ${formatNumber(trade['exit_price'], decimals: 5)}' : ''}',
                style: const TextStyle(
                  color: Colors.white60,
                  fontSize: 12,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                status == 'OPEN'
                    ? 'Autonomous PAPER trade is being monitored by the backend.'
                    : 'Settled PAPER outcome: ${trade['result'] ?? status}.',
                style: const TextStyle(
                  color: Colors.white54,
                  fontSize: 11,
                ),
              ),
            ],
          ),
        );
      }

      Widget learningWatcherCard(
        Map<String, dynamic> watcher,
      ) {
        final candidate =
            watcher['candidate'] is Map
                ? Map<String, dynamic>.from(
                    watcher['candidate'],
                  )
                : <String, dynamic>{};

        final market =
            watcher['market']?.toString() ??
                watcher['symbol']?.toString() ??
                '-';

        final direction =
            watcher['direction']
                    ?.toString()
                    .toUpperCase() ??
                '-';

        final status =
            watcher['status']
                    ?.toString()
                    .toUpperCase() ??
                '-';

        final deepStatus =
            watcher['deep_status']
                    ?.toString()
                    .toUpperCase() ??
                '-';

        final quality =
            candidate['quality_tier']
                    ?.toString() ??
                '-';

        final fastScore =
            candidate['smart_fast_score'];

        final quant =
            watcher['last_quant_confidence'];

        return glassCard(
          glow: status == 'SHADOW_WATCH'
              ? Colors.amberAccent
              : cs.primary,
          child: Column(
            crossAxisAlignment:
                CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      '$market  $direction',
                      style: const TextStyle(
                        fontWeight:
                            FontWeight.w900,
                      ),
                    ),
                  ),
                  pill(
                    status,
                    color:
                        status == 'SHADOW_WATCH'
                            ? Colors.amberAccent
                            : cs.primary,
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  pill(
                    'Deep $deepStatus',
                    icon:
                        Icons.fact_check_outlined,
                  ),
                  pill(
                    'Quality $quality',
                    icon: Icons.grade_outlined,
                  ),
                  if (fastScore is num)
                    pill(
                      'Fast ${formatNumber(fastScore, decimals: 0)}',
                      icon:
                          Icons.speed_rounded,
                    ),
                  if (quant is num)
                    pill(
                      'Quant ${formatPercent(quant)}%',
                      icon: Icons
                          .analytics_outlined,
                    ),
                ],
              ),
              if (watcher['last_error'] !=
                  null) ...[
                const SizedBox(height: 10),
                Text(
                  watcher['last_error']
                      .toString(),
                  maxLines: 3,
                  overflow:
                      TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white38,
                    fontSize: 10,
                  ),
                ),
              ],
            ],
          ),
        );
      }

      Widget overnightDemoCard() {
        Color stateColor;
        IconData stateIcon;

        if (overnightComplete) {
          stateColor = Colors.greenAccent;
          stateIcon = Icons.flag_circle_rounded;
        } else if (overnightActive) {
          stateColor = Colors.greenAccent;
          stateIcon = Icons.nightlight_round;
        } else if (overnightDraining) {
          stateColor = Colors.amberAccent;
          stateIcon = Icons.hourglass_bottom_rounded;
        } else {
          stateColor = Colors.white54;
          stateIcon = Icons.bedtime_outlined;
        }

        final manager =
            overnight['manager'] is Map
                ? Map<String, dynamic>.from(
                    overnight['manager'],
                  )
                : <String, dynamic>{};

        final progressMessage =
            manager['progress_message']
                    ?.toString() ??
                'Waiting for the next scan';

        final currentTrade =
            overnight['current_trade'] is Map
                ? Map<String, dynamic>.from(
                    overnight['current_trade'],
                  )
                : null;

        return glassCard(
          glow: stateColor,
          child: Column(
            crossAxisAlignment:
                CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Container(
                    width: 46,
                    height: 46,
                    decoration: BoxDecoration(
                      color: stateColor.withValues(
                        alpha: .10,
                      ),
                      borderRadius:
                          BorderRadius.circular(15),
                    ),
                    child: Icon(
                      stateIcon,
                      color: stateColor,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment:
                          CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'OVERNIGHT IG DEMO',
                          style: TextStyle(
                            fontSize: 17,
                            fontWeight:
                                FontWeight.w900,
                          ),
                        ),
                        const SizedBox(height: 3),
                        Text(
                          overnightActive
                              ? 'Server-side autonomous demo trading is active'
                              : overnightDraining
                                  ? 'New entries stopped • current demo trade is settling'
                                  : overnightComplete
                                      ? 'Phase target reached • no more demo entries required'
                                      : 'Ready for a server-side overnight run',
                          style: const TextStyle(
                            color:
                                Colors.white60,
                            fontSize: 11,
                          ),
                        ),
                      ],
                    ),
                  ),
                  pill(
                    overnightState,
                    color: stateColor,
                  ),
                ],
              ),
              const SizedBox(height: 16),
              Row(
                children: [
                  statTile(
                    'Phase',
                    '$phaseAccepted / $phaseTarget',
                    Icons.flag_outlined,
                    valueColor:
                        overnightComplete
                            ? Colors.greenAccent
                            : Colors.white,
                  ),
                  const SizedBox(width: 10),
                  statTile(
                    'IG positions',
                    '$brokerPositions',
                    Icons.swap_horiz_rounded,
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Row(
                children: [
                  statTile(
                    'W / L',
                    '$overnightWins / $overnightLosses',
                    Icons.fact_check_outlined,
                  ),
                  const SizedBox(width: 10),
                  statTile(
                    'Win rate',
                    '${formatNumber(overnightWinRate, decimals: 1)}%',
                    Icons.insights_rounded,
                    valueColor:
                        overnightWins > 0
                            ? Colors.greenAccent
                            : Colors.white,
                  ),
                ],
              ),
              const SizedBox(height: 14),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: (
                    brokerSyncState == 'SYNCED'
                        ? Colors.greenAccent
                        : brokerSyncState == 'ERROR'
                            ? Colors.redAccent
                            : Colors.amberAccent
                  ).withValues(alpha: .06),
                  borderRadius:
                      BorderRadius.circular(14),
                  border: Border.all(
                    color: (
                      brokerSyncState == 'SYNCED'
                          ? Colors.greenAccent
                          : brokerSyncState == 'ERROR'
                              ? Colors.redAccent
                              : Colors.amberAccent
                    ).withValues(alpha: .20),
                  ),
                ),
                child: Row(
                  children: [
                    Icon(
                      brokerSyncState == 'SYNCED'
                          ? Icons.sync_rounded
                          : brokerSyncState == 'ERROR'
                              ? Icons.sync_problem_rounded
                              : Icons.sync_lock_rounded,
                      color:
                          brokerSyncState == 'SYNCED'
                              ? Colors.greenAccent
                              : brokerSyncState == 'ERROR'
                                  ? Colors.redAccent
                                  : Colors.amberAccent,
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment:
                            CrossAxisAlignment.start,
                        children: [
                          Text(
                            'Backend ↔ IG DEMO: $brokerSyncState',
                            style: const TextStyle(
                              fontWeight:
                                  FontWeight.w900,
                              fontSize: 12,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            brokerSyncAge is num
                                ? 'Last broker reconciliation ${formatCountdownSeconds(brokerSyncAge)} ago • $brokerPositions open position(s)'
                                : 'Waiting for broker reconciliation • $brokerPositions open position(s)',
                            style:
                                const TextStyle(
                              color:
                                  Colors.white54,
                              fontSize: 10,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              if (brokerPositionRows.isNotEmpty) ...[
                const SizedBox(height: 10),
                ...brokerPositionRows
                    .take(3)
                    .map(
                      (position) => Padding(
                        padding:
                            const EdgeInsets.only(
                          bottom: 8,
                        ),
                        child: Container(
                          width:
                              double.infinity,
                          padding:
                              const EdgeInsets
                                  .symmetric(
                            horizontal: 12,
                            vertical: 10,
                          ),
                          decoration:
                              BoxDecoration(
                            color: Colors.white
                                .withValues(
                              alpha: .025,
                            ),
                            borderRadius:
                                BorderRadius
                                    .circular(
                              12,
                            ),
                            border: Border.all(
                              color: Colors.white
                                  .withValues(
                                alpha: .05,
                              ),
                            ),
                          ),
                          child: Row(
                            children: [
                              const Icon(
                                Icons
                                    .account_balance_rounded,
                                size: 17,
                                color: Color(
                                  0xFF65E6D3,
                                ),
                              ),
                              const SizedBox(
                                width: 9,
                              ),
                              Expanded(
                                child: Text(
                                  '${position['symbol'] ?? position['market'] ?? 'IG'} '
                                  '${position['direction'] ?? ''}',
                                  style:
                                      const TextStyle(
                                    fontWeight:
                                        FontWeight
                                            .w800,
                                    fontSize: 11,
                                  ),
                                ),
                              ),
                              Text(
                                '${position['size'] ?? '-'} @ ${formatNumber(position['entry_level'], decimals: 5)}',
                                style:
                                    const TextStyle(
                                  color:
                                      Colors.white60,
                                  fontSize: 10,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
              ],
              const SizedBox(height: 4),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(
                    alpha: .035,
                  ),
                  borderRadius:
                      BorderRadius.circular(14),
                  border: Border.all(
                    color:
                        Colors.white.withValues(
                      alpha: .05,
                    ),
                  ),
                ),
                child: Column(
                  crossAxisAlignment:
                      CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Scanner: ${overnight['scanner_universe'] ?? 'CURATED_LEARNING_FX'}',
                      style: const TextStyle(
                        fontWeight:
                            FontWeight.w700,
                        fontSize: 11,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      progressMessage,
                      style: const TextStyle(
                        color: Colors.white54,
                        fontSize: 11,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Remaining Phase-1 entries: $phaseRemaining • AI floor ${formatNumber(overnight['ai_min_confidence_pct'] ?? aiFloor, decimals: 0)}%',
                      style: const TextStyle(
                        color: Colors.white38,
                        fontSize: 10,
                      ),
                    ),
                  ],
                ),
              ),
              if (currentTrade != null) ...[
                const SizedBox(height: 12),
                Text(
                  'Current: ${currentTrade['symbol'] ?? currentTrade['market'] ?? '-'} '
                  '${currentTrade['direction'] ?? ''} • '
                  'closes in ${formatEpochCountdown(currentTrade['scheduled_close_at'] ?? currentTrade['settlement_due_at'])}',
                  style: const TextStyle(
                    color: Colors.white70,
                    fontWeight: FontWeight.w700,
                    fontSize: 11,
                  ),
                ),
              ],
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: overnightDemoBusy ||
                              overnightComplete
                          ? null
                          : overnightActive ||
                                  overnightDraining
                              ? stopOvernightDemo
                              : startOvernightDemo,
                      icon: overnightDemoBusy
                          ? const SizedBox(
                              width: 18,
                              height: 18,
                              child:
                                  CircularProgressIndicator(
                                strokeWidth: 2,
                              ),
                            )
                          : Icon(
                              overnightActive ||
                                      overnightDraining
                                  ? Icons.stop_circle_outlined
                                  : Icons.play_circle_fill_rounded,
                            ),
                      label: Text(
                        overnightDemoBusy
                            ? 'Please wait...'
                            : overnightActive ||
                                    overnightDraining
                                ? 'Stop new overnight entries'
                                : 'START OVERNIGHT DEMO',
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  IconButton.filledTonal(
                    tooltip:
                        'Refresh overnight status',
                    onPressed:
                        loadOvernightDemoStatus,
                    icon: const Icon(
                      Icons.refresh_rounded,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              const Text(
                'IG DEMO ONLY • Live-money execution remains disabled. '
                'IG is the broker source of truth; the app re-syncs from the backend whenever it opens or resumes.',
                style: TextStyle(
                  color: Colors.white38,
                  fontSize: 10,
                  height: 1.35,
                ),
              ),
            ],
          ),
        );
      }

      return ListView(
        padding:
            const EdgeInsets.fromLTRB(
          16,
          4,
          16,
          120,
        ),
        children: [
          sectionTitle(
            'Overnight Demo Mode',
            subtitle:
                'One-tap server-side IG DEMO learning • phone can be locked or closed',
          ),
          overnightDemoCard(),
          const SizedBox(height: 22),
          sectionTitle(
            'Autonomous AI PAPER Learning',
            subtitle:
                'AI40 shadow promotion • monitoring only on your phone',
          ),
          glassCard(
            glow: engineEnabled
                ? Colors.greenAccent
                : Colors.amberAccent,
            child: Column(
              crossAxisAlignment:
                  CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(
                      engineEnabled
                          ? Icons
                              .smart_toy_rounded
                          : Icons
                              .pause_circle_outline,
                      color: engineEnabled
                          ? Colors.greenAccent
                          : Colors.amberAccent,
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        engineEnabled
                            ? 'AI learning is ACTIVE'
                            : 'AI learning is PAUSED',
                        style: const TextStyle(
                          fontSize: 16,
                          fontWeight:
                              FontWeight.w900,
                        ),
                      ),
                    ),
                    pill(
                      liveExecution
                          ? 'LIVE'
                          : 'PAPER ONLY',
                      color: liveExecution
                          ? Colors.redAccent
                          : Colors.greenAccent,
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Text(
                  mode,
                  style: const TextStyle(
                    color: Colors.white70,
                    fontWeight:
                        FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  'AI directional floor: ${formatNumber(aiFloor, decimals: 0)}% • N30 disabled for this experiment',
                  style: const TextStyle(
                    color: Colors.white54,
                    fontSize: 11,
                  ),
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    statTile(
                      'Watchers',
                      '$activeWatchers',
                      Icons.visibility_outlined,
                    ),
                    const SizedBox(width: 10),
                    statTile(
                      'Open trades',
                      '$openTrades',
                      Icons
                          .receipt_long_outlined,
                      valueColor:
                          (openTrades is num &&
                                  openTrades > 0)
                              ? Colors
                                  .lightBlueAccent
                              : Colors.white,
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    statTile(
                      'Paper balance',
                      formatMoney(
                        learningBalance,
                      ),
                      Icons
                          .account_balance_wallet_outlined,
                    ),
                    const SizedBox(width: 10),
                    statTile(
                      'AI40 floor',
                      '${formatNumber(aiFloor, decimals: 0)}%',
                      Icons
                          .verified_outlined,
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child:
                          FilledButton.icon(
                        onPressed:
                            aiLearningBusy
                                ? null
                                : runAiLearningNow,
                        icon:
                            aiLearningBusy
                                ? const SizedBox(
                                    width: 16,
                                    height: 16,
                                    child:
                                        CircularProgressIndicator(
                                      strokeWidth:
                                          2,
                                    ),
                                  )
                                : const Icon(
                                    Icons
                                        .auto_awesome_rounded,
                                  ),
                        label: Text(
                          aiLearningBusy
                              ? 'AI evaluating...'
                              : 'Run AI cycle now',
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    IconButton.filledTonal(
                      tooltip:
                          'Refresh AI learning',
                      onPressed: () async {
                        await loadAutoDashboard();
                        await loadAiLearningStatus();
                      },
                      icon: const Icon(
                        Icons.refresh_rounded,
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          if (openAiTrade != null) ...[
            const SizedBox(height: 20),
            sectionTitle(
              'Current AI PAPER trade',
              subtitle:
                  'One open learning trade maximum',
            ),
            learningTradeCard(
              openAiTrade,
            ),
          ] else if (aiTrades.isNotEmpty) ...[
            const SizedBox(height: 20),
            sectionTitle(
              'Latest AI PAPER outcome',
            ),
            learningTradeCard(
              aiTrades.first,
            ),
          ],
          if (aiLearningLastRun !=
              null) ...[
            const SizedBox(height: 20),
            sectionTitle(
              'Last AI decision',
            ),
            glassCard(
              glow: aiLearningLastRun![
                          'status']
                      ?.toString() ==
                  'PAPER_TRADE_OPENED'
                  ? Colors.greenAccent
                  : Colors.amberAccent,
              child: Column(
                crossAxisAlignment:
                    CrossAxisAlignment.start,
                children: [
                  Text(
                    aiLearningLastRun![
                                'status']
                            ?.toString() ??
                        '-',
                    style: const TextStyle(
                      fontWeight:
                          FontWeight.w900,
                      fontSize: 15,
                    ),
                  ),
                  if (aiLearningLastRun![
                          'selected']
                      is Map) ...[
                    const SizedBox(
                      height: 8,
                    ),
                    Builder(
                      builder: (_) {
                        final selected =
                            Map<String,
                                dynamic>.from(
                          aiLearningLastRun![
                              'selected'],
                        );

                        return Text(
                          '${selected['market'] ?? selected['symbol'] ?? '-'} '
                          '${selected['candidate_direction'] ?? ''} • '
                          'AI ${selected['model_ai_directional_confidence_pct'] ?? '-'}% • '
                          'Quant ${selected['quant_confidence_pct'] ?? '-'}%',
                          style:
                              const TextStyle(
                            color:
                                Colors.white70,
                          ),
                        );
                      },
                    ),
                  ],
                  if (aiLearningLastRun![
                          'error'] !=
                      null) ...[
                    const SizedBox(
                      height: 8,
                    ),
                    Text(
                      aiLearningLastRun![
                              'error']
                          .toString(),
                      style:
                          const TextStyle(
                        color:
                            Colors.redAccent,
                        fontSize: 11,
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
          const SizedBox(height: 20),
          sectionTitle(
            'AI learning watchers',
            subtitle:
                'A/A+ shadow candidates are isolated from normal production entries',
          ),
          if (aiLearningWatchers.isEmpty)
            glassCard(
              child: const Text(
                'No AI-learning watchers yet. Auto Manager will supply new candidates automatically.',
                style: TextStyle(
                  color: Colors.white54,
                ),
              ),
            )
          else
            ...aiLearningWatchers
                .take(6)
                .map(
                  (watcher) => Padding(
                    padding:
                        const EdgeInsets.only(
                      bottom: 10,
                    ),
                    child:
                        learningWatcherCard(
                      watcher,
                    ),
                  ),
                ),
          const SizedBox(height: 20),
          sectionTitle(
            'Jasong AI Copilot',
            subtitle:
                'Advisory analysis of PAPER performance and risk evidence',
          ),
          glassCard(
            glow: cs.secondary,
            child: Column(
              children: [
                TextField(
                  controller:
                      copilotController,
                  minLines: 3,
                  maxLines: 5,
                  decoration:
                      const InputDecoration(
                    hintText:
                        'Ask Jasong AI about trades, watchers, losses or confidence buckets...',
                    prefixIcon: Icon(
                      Icons
                          .psychology_alt_rounded,
                    ),
                  ),
                ),
                const SizedBox(
                  height: 10,
                ),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: copilotBusy
                        ? null
                        : () =>
                            askJasongCopilot(),
                    icon: copilotBusy
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child:
                                CircularProgressIndicator(
                              strokeWidth: 2,
                            ),
                          )
                        : const Icon(
                            Icons
                                .auto_awesome_rounded,
                          ),
                    label: Text(
                      copilotBusy
                          ? 'Analysing...'
                          : 'Ask Jasong AI',
                    ),
                  ),
                ),
                const SizedBox(
                  height: 8,
                ),
                SizedBox(
                  width: double.infinity,
                  child:
                      OutlinedButton.icon(
                    onPressed: copilotBusy
                        ? null
                        : runOvernightReview,
                    icon: const Icon(
                      Icons
                          .nights_stay_rounded,
                    ),
                    label: const Text(
                      'Analyse overnight performance',
                    ),
                  ),
                ),
              ],
            ),
          ),
          if (copilotAnswer
              .isNotEmpty) ...[
            const SizedBox(height: 12),
            glassCard(
              child: SelectableText(
                copilotAnswer,
                style: const TextStyle(
                  color: Colors.white70,
                  height: 1.45,
                ),
              ),
            ),
          ],
          const SizedBox(height: 20),
          sectionTitle(
            'Learning thresholds',
            subtitle:
                'Experimental PAPER eligibility — not win probabilities',
          ),
          glassCard(
            child: const Column(
              children: [
                _MidnightRuleRow(
                  'AI40',
                  '40%',
                  'Directional model-AI + live direction agreement',
                ),
                Divider(height: 24),
                _MidnightRuleRow(
                  'EM',
                  'Experimental',
                  'High-quality shadow promoted for AI PAPER learning only',
                ),
                Divider(height: 24),
                _MidnightRuleRow(
                  'SHADOW',
                  'ON',
                  'Rejected opportunities remain learning evidence',
                ),
                Divider(height: 24),
                _MidnightRuleRow(
                  'IG DEMO BROKER',
                  'ON',
                  'Demo broker orders are mirrored to IG • live money remains OFF',
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          sectionTitle(
            'Normal observation portfolio',
          ),
          if (serverWatchers.isEmpty)
            glassCard(
              child: const Text(
                'No normal active watchers loaded.',
                style: TextStyle(
                  color: Colors.white54,
                ),
              ),
            )
          else
            ...serverWatchers
                .take(6)
                .map(
                  (w) => Padding(
                    padding:
                        const EdgeInsets.only(
                      bottom: 10,
                    ),
                    child: watcherCard(w),
                  ),
                ),
        ],
      );
    }

    Widget settingsPage() {
      final overviewStatus = systemOverview?['status']?.toString() ??
          systemOverview?['overall_status']?.toString() ??
          (autoDashboard != null ? 'ONLINE' : 'CHECKING');
      return ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 120),
        children: [
          sectionTitle('Trading preferences'),
          glassCard(
            child: Column(
              children: [
                TextField(
                  controller: symbol,
                  decoration: const InputDecoration(
                    labelText: 'Market symbol',
                    prefixIcon: Icon(Icons.currency_exchange_rounded),
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: balance,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(
                    labelText: 'Paper balance',
                    prefixIcon: Icon(Icons.account_balance_wallet_outlined),
                  ),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  value: risk,
                  decoration: const InputDecoration(
                    labelText: 'Risk mode',
                    prefixIcon: Icon(Icons.shield_outlined),
                  ),
                  items: ['Conservative', 'Balanced', 'Aggressive']
                      .map((value) => DropdownMenuItem<String>(value: value, child: Text(value)))
                      .toList(),
                  onChanged: busy
                      ? null
                      : (value) => setState(() => risk = value ?? 'Balanced'),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.tonalIcon(
                    onPressed: busy ? null : refreshSignal,
                    icon: const Icon(Icons.refresh_rounded),
                    label: const Text('Refresh current signal'),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          sectionTitle('Auto Manager'),
          glassCard(
            glow: autoOn ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E),
            child: Column(
              children: [
                Row(
                  children: [
                    Icon(autoOn ? Icons.bolt_rounded : Icons.pause_circle_outline, color: autoOn ? const Color(0xFF67F0C1) : const Color(0xFFFFD75E)),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(autoOn ? 'Auto Manager is running' : 'Auto Manager is stopped', style: const TextStyle(fontWeight: FontWeight.w900)),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    onPressed: autoManagerBusy ? null : (autoOn ? stopAutoMode : startAutoMode),
                    icon: Icon(autoOn ? Icons.stop_circle_outlined : Icons.play_circle_outline),
                    label: Text(autoOn ? 'Stop Auto Mode' : 'Start Auto Mode'),
                  ),
                ),
                const SizedBox(height: 8),
                SizedBox(
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    onPressed: autoManagerBusy ? null : runAutoManagerNow,
                    icon: const Icon(Icons.bolt_rounded),
                    label: const Text('Run one cycle now'),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          sectionTitle('System health'),
          glassCard(
            child: Column(
              children: [
                _MidnightSystemRow('Backend', overviewStatus),
                const Divider(height: 24),
                _MidnightSystemRow('API endpoint', apiBase.replaceFirst('https://', '')),
                const Divider(height: 24),
                const _MidnightSystemRow('Execution', 'PAPER ONLY'),
                const Divider(height: 24),
                const _MidnightSystemRow('Live broker execution', 'OFF'),
              ],
            ),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              onPressed: systemDiagnosticBusy ? null : runSystemDiagnostic,
              icon: systemDiagnosticBusy
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.health_and_safety_outlined),
              label: Text(systemDiagnosticBusy ? 'Running diagnostic...' : 'Run system diagnostic'),
            ),
          ),
          if (systemDiagnostic != null) ...[
            const SizedBox(height: 12),
            glassCard(
              child: SelectableText(
                const JsonEncoder.withIndent('  ').convert(systemDiagnostic),
                style: const TextStyle(fontFamily: 'monospace', fontSize: 11, color: Colors.white60),
              ),
            ),
          ],
        ],
      );
    }

    final pages = <Widget>[
      dashboardPage(),
      marketsPage(),
      tradesPage(),
      aiPage(),
      settingsPage(),
    ];

    return Scaffold(
      extendBody: true,
      appBar: AppBar(
        toolbarHeight: 76,
        titleSpacing: 16,
        title: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                gradient: LinearGradient(colors: [cs.primary, cs.secondary]),
                borderRadius: BorderRadius.circular(14),
              ),
              child: const Icon(Icons.auto_graph_rounded, color: Color(0xFF041014)),
            ),
            const SizedBox(width: 11),
            const Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Jasong AI Trader', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w900)),
                  SizedBox(height: 2),
                  Text('V6.6.9 • Evidence-Sync DEMO', style: TextStyle(fontSize: 10, color: Colors.white54, letterSpacing: .35)),
                ],
              ),
            ),
          ],
        ),
        actions: [
          IconButton.filledTonal(
            tooltip: 'Refresh dashboard',
            onPressed: () async {
              await loadAutoDashboard();
              await loadSystemOverview();
              await loadAiLearningStatus();
              await loadOvernightDemoStatus();
              await refreshServerWatchers();
            },
            icon: const Icon(Icons.refresh_rounded),
          ),
          const SizedBox(width: 10),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          await loadAutoDashboard();
          await loadSystemOverview();
          await loadAiLearningStatus();
          await loadOvernightDemoStatus();
          await refreshServerWatchers();
          if (selectedTab == 0) {
            await refreshSignal();
          }
        },
        child: SafeArea(
          top: false,
          child: pages[selectedTab],
        ),
      ),
      bottomNavigationBar: NavigationBar(
        height: 72,
        selectedIndex: selectedTab,
        onDestinationSelected: (index) => setState(() => selectedTab = index),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.dashboard_outlined), selectedIcon: Icon(Icons.dashboard_rounded), label: 'Home'),
          NavigationDestination(icon: Icon(Icons.radar_outlined), selectedIcon: Icon(Icons.radar_rounded), label: 'Markets'),
          NavigationDestination(icon: Icon(Icons.receipt_long_outlined), selectedIcon: Icon(Icons.receipt_long_rounded), label: 'Trades'),
          NavigationDestination(icon: Icon(Icons.psychology_alt_outlined), selectedIcon: Icon(Icons.psychology_alt_rounded), label: 'AI'),
          NavigationDestination(icon: Icon(Icons.tune_outlined), selectedIcon: Icon(Icons.tune_rounded), label: 'Settings'),
        ],
      ),
    );
  }

}


class _MidnightValue extends StatelessWidget {
  final String label;
  final String value;

  const _MidnightValue(this.label, this.value);

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 12),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: .035),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white.withValues(alpha: .05)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontSize: 10,
              color: Color(0x73FFFFFF),
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              fontWeight: FontWeight.w900,
              fontSize: 15,
            ),
          ),
        ],
      ),
    );
  }
}

Widget _midnightValue(String label, String value) {
  return _MidnightValue(label, value);
}

class _MidnightRuleRow extends StatelessWidget {
  final String title;
  final String value;
  final String subtitle;

  const _MidnightRuleRow(this.title, this.value, this.subtitle);

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: 34,
          height: 34,
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.primary.withValues(alpha: .10),
            borderRadius: BorderRadius.circular(11),
          ),
          child: Icon(
            Icons.auto_awesome_rounded,
            size: 17,
            color: Theme.of(context).colorScheme.primary,
          ),
        ),
        const SizedBox(width: 11),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w800)),
              const SizedBox(height: 2),
              Text(subtitle, style: const TextStyle(color: Colors.white54, fontSize: 11, height: 1.25)),
            ],
          ),
        ),
        const SizedBox(width: 10),
        Text(value, style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 15)),
      ],
    );
  }
}

class _MidnightSystemRow extends StatelessWidget {
  final String label;
  final String value;

  const _MidnightSystemRow(this.label, this.value);

  @override
  Widget build(BuildContext context) {
    final good = !value.toUpperCase().contains('OFFLINE') &&
        !value.toUpperCase().contains('ERROR') &&
        !value.toUpperCase().contains('FAILED');
    return Row(
      children: [
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: good ? const Color(0xFF67F0C1) : const Color(0xFFFF6B75),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(child: Text(label, style: const TextStyle(color: Colors.white70))),
        const SizedBox(width: 8),
        Flexible(
          child: Text(
            value,
            textAlign: TextAlign.right,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 11),
          ),
        ),
      ],
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
