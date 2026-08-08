import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:fl_chart/fl_chart.dart';

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
  final symbol = TextEditingController(text: 'EURUSD=X');
  final balance = TextEditingController(text: '10000');
  String risk = 'Balanced';
  String apiBase = const String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000',
  );

  Map<String,dynamic>? sig;
  Map<String,dynamic>? bt;
  bool busy = false;
  String? error;

  Future<Map<String,dynamic>> getJson(Uri uri) async {
    final r = await http.get(uri).timeout(const Duration(seconds: 25));
    if (r.statusCode != 200) throw Exception('Server ${r.statusCode}: ${r.body}');
    return jsonDecode(r.body);
  }

  Future<void> refreshSignal() async {
    setState(() {busy=true; error=null;});
    try {
      final uri = Uri.parse('$apiBase/signal').replace(queryParameters: {
        'symbol':symbol.text,
        'risk_mode':risk,
        'balance':balance.text,
      });
      sig = await getJson(uri);
    } catch(e) {
      error = e.toString();
    } finally {
      setState(() => busy=false);
    }
  }

  Future<void> runBacktest() async {
    setState(() {busy=true; error=null;});
    try {
      final uri = Uri.parse('$apiBase/backtest').replace(queryParameters: {
        'symbol':symbol.text,
        'risk_mode':risk,
        'starting_balance':balance.text,
      });
      bt = await getJson(uri);
    } catch(e) {
      error = e.toString();
    } finally {
      setState(() => busy=false);
    }
  }

  Future<void> recordPaperTrade() async {
    if (sig == null || !['BUY','SELL'].contains(sig!['decision'])) return;
    setState(() {busy=true; error=null;});
    try {
      final uri = Uri.parse('$apiBase/paper-trades').replace(queryParameters: {
        'symbol':symbol.text,
        'direction':sig!['decision'].toString(),
        'confidence':sig!['confidence'].toString(),
        'entry_price':sig!['price'].toString(),
        'stake':sig!['suggested_paper_stake'].toString(),
      });
      final r = await http.post(uri).timeout(const Duration(seconds: 20));
      if (r.statusCode != 200) throw Exception(r.body);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Paper trade recorded')),
        );
      }
    } catch(e) {
      error=e.toString();
    } finally {
      setState(() => busy=false);
    }
  }

  @override
  void initState() {
    super.initState();
    Future.microtask(refreshSignal);
  }

  Color decisionColor(String d) {
    if (d=='BUY') return Colors.greenAccent;
    if (d=='SELL') return Colors.redAccent;
    return Colors.amberAccent;
  }

  Widget metric(String label, String value) {
    return Expanded(child: Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(children:[
          Text(label, style: const TextStyle(fontSize:12)),
          const SizedBox(height:6),
          Text(value, textAlign:TextAlign.center,
            style: const TextStyle(fontSize:19,fontWeight:FontWeight.bold)),
        ]),
      ),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final d = sig?['decision']?.toString() ?? 'WAIT';
    final conf = (((sig?['confidence'] ?? 0) as num).toDouble()*100).toStringAsFixed(1);
    final up = (((sig?['combined_up_probability'] ?? 0) as num).toDouble()*100).toStringAsFixed(1);
    final curve = (bt?['equity_curve'] as List?) ?? const [];

    return Scaffold(
      appBar: AppBar(
        title: const Text('Jasong AI Trader V3'),
        actions:[IconButton(onPressed:busy?null:refreshSignal, icon:const Icon(Icons.refresh))]
      ),
      body: RefreshIndicator(
        onRefresh: refreshSignal,
        child: ListView(
          padding: const EdgeInsets.all(14),
          children:[
            const Text('AI-assisted paper trading', style:TextStyle(fontWeight:FontWeight.bold)),
            const SizedBox(height:10),
            TextField(
              controller:symbol,
              decoration:const InputDecoration(labelText:'Market symbol',border:OutlineInputBorder()),
            ),
            const SizedBox(height:10),
            TextField(
              controller:balance,
              keyboardType:TextInputType.number,
              decoration:const InputDecoration(labelText:'Paper balance',border:OutlineInputBorder()),
            ),
            const SizedBox(height:10),
            DropdownButtonFormField<String>(
              value:risk,
              decoration:const InputDecoration(labelText:'Risk mode',border:OutlineInputBorder()),
              items:['Conservative','Balanced','Aggressive']
                .map((x)=>DropdownMenuItem(value:x,child:Text(x))).toList(),
              onChanged:(v)=>setState(()=>risk=v ?? 'Balanced'),
            ),
            const SizedBox(height:14),
            Card(
              child:Padding(
                padding:const EdgeInsets.all(18),
                child:Column(children:[
                  Text(d,style:TextStyle(fontSize:44,fontWeight:FontWeight.w900,color:decisionColor(d))),
                  const SizedBox(height:6),
                  Text(sig?['reason']?.toString() ?? 'Waiting for signal...',textAlign:TextAlign.center),
                ]),
              ),
            ),
            Row(children:[
              metric('Confidence','$conf%'),
              metric('AI up','$up%'),
            ]),
            Row(children:[
              metric('Price','${sig?['price'] ?? '-'}'),
              metric('RSI','${sig?['rsi'] ?? '-'}'),
            ]),
            Row(children:[
              metric('Paper stake','${sig?['suggested_paper_stake'] ?? '-'}'),
              metric('Mode',risk),
            ]),
            const SizedBox(height:10),
            FilledButton.icon(
              onPressed:busy?null:refreshSignal,
              icon:const Icon(Icons.psychology),
              label:const Text('Refresh AI Signal'),
            ),
            const SizedBox(height:8),
            OutlinedButton.icon(
              onPressed:busy?null:runBacktest,
              icon:const Icon(Icons.query_stats),
              label:const Text('Run Backtest'),
            ),
            const SizedBox(height:8),
            OutlinedButton.icon(
              onPressed:(busy || !['BUY','SELL'].contains(d))?null:recordPaperTrade,
              icon:const Icon(Icons.edit_note),
              label:const Text('Record Paper Trade'),
            ),
            if (busy) const Padding(
              padding:EdgeInsets.all(16),
              child:Center(child:CircularProgressIndicator()),
            ),
            if (error != null) Padding(
              padding:const EdgeInsets.only(top:10),
              child:Text(error!,style:const TextStyle(color:Colors.redAccent)),
            ),
            if (bt != null) ...[
              const SizedBox(height:18),
              const Text('Backtest',style:TextStyle(fontSize:20,fontWeight:FontWeight.bold)),
              Row(children:[
                metric('Trades','${bt!['trades']}'),
                metric('Win rate','${(((bt!['win_rate'] ?? 0) as num)*100).toStringAsFixed(1)}%'),
              ]),
              Row(children:[
                metric('Return','${(((bt!['return_pct'] ?? 0) as num)*100).toStringAsFixed(1)}%'),
                metric('Max DD','${(((bt!['max_drawdown'] ?? 0) as num)*100).toStringAsFixed(1)}%'),
              ]),
              if (curve.isNotEmpty) SizedBox(
                height:220,
                child:LineChart(LineChartData(
                  titlesData:const FlTitlesData(show:false),
                  borderData:FlBorderData(show:true),
                  lineBarsData:[LineChartBarData(
                    isCurved:true,
                    dotData:const FlDotData(show:false),
                    spots:[
                      for(int i=0;i<curve.length;i++)
                        FlSpot(i.toDouble(),(curve[i]['balance'] as num).toDouble())
                    ],
                  )],
                )),
              ),
            ],
            const SizedBox(height:16),
            const Card(
              child:Padding(
                padding:EdgeInsets.all(14),
                child:Text(
                  'Safety: no Martingale, no forced daily-profit target, no live IQ Option execution, '
                  'and no broker password stored in the app. Historical or model results do not guarantee profit.'
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
