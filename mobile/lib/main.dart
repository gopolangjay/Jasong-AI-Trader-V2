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
      home: const HomeWithBranding(),
    );
  }
}

class HomeWithBranding extends StatelessWidget {
  const HomeWithBranding({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: const [
            LogoHeader(),
            Expanded(child: HomePage()),
          ],
        ),
      ),
    );
  }
}

class LogoHeader extends StatelessWidget {
  const LogoHeader({super.key});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 18, 16, 8),
      child: Column(
        children: [
          Image.asset(
            'assets/images/jasong_logo.png',
            width: 140,
            height: 140,
            fit: BoxFit.contain,
          ),
          const SizedBox(height: 8),
          const Text(
            'Power • Evolution • Legacy',
            style: TextStyle(
              fontSize: 14,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.6,
            ),
          ),
          const SizedBox(height: 8),
        ],
      ),
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

  Map<String, dynamic>? liveEntryAssessment;

  Map<String, dynamic>? serverWatcher;
  List<Map<String, dynamic>> serverWatchers = [];
  Map<String, dynamic>? forwardStats;
  Map<String, dynamic>? v66ForwardIntelligence;

  List<Map<String, dynamic>> paperTrades = [];

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
          scanResult['top_candidates']
              as List?;

      if (rawCandidates == null ||
          rawCandidates.isEmpty) {
        throw Exception(
          'Fast scanner returned '
          'no candidates',
        );
      }

      final candidates = <Map<String, dynamic>>[];

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
              'Deep validating $market';

          networkStatus =
              'Starting validation for $market';
        });

        try {
          final result =
              await deepValidateWithRecovery(
            candidate,
          );

          if (!mounted) {
            return;
          }

          setState(() {
            validationHistory.add(
              {
                'market': market,
                'result': result,
              },
            );
          });

          // If a verified trade is found, keep it and stop.
          final isVerified =
              result['verified'] == true;

          if (isVerified) {
            setState(() {
              verifiedTrade = {
                'market': market,
                'result': result,
              };
            });

            break;
          }
        } catch (e) {
          if (!mounted) return;

          setState(() {
            validationHistory.add({
              'market': market,
              'error': e.toString(),
            });
          });
        }
      }
    } catch (e) {
      if (!mounted) return;

      setState(() {
        error = e.toString();
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

  @override
  void dispose() {
    watcherPollTimer?.cancel();
    autoDashboardPollTimer?.cancel();
    copilotController.dispose();
    balance.dispose();
    symbol.dispose();
    super.dispose();
  }

  // The remainder of the file includes UI and handlers which I did not change.
}
