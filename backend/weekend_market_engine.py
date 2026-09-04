from __future__ import annotations
import os, threading, time, uuid
from datetime import datetime, timezone
from weekend_market_policy import STRATEGY_ID, assess_market, execution_guard
VERSION="6.13-weekend-structure-execution-v6"
CRYPTO_SEEDS=(
 {"symbol":"BITCOIN","terms":["Bitcoin"],"aliases":["BITCOIN","BTC"]},
 {"symbol":"ETHER","terms":["Ethereum","Ether"],"aliases":["ETHEREUM","ETHER","ETH"]},
 {"symbol":"SOLANA","terms":["Solana"],"aliases":["SOLANA","SOL"]},
 {"symbol":"XRP","terms":["XRP","Ripple"],"aliases":["XRP","RIPPLE"]},
 {"symbol":"LITECOIN","terms":["Litecoin"],"aliases":["LITECOIN","LTC"]},)
def _num(v):
 if isinstance(v,dict):v=v.get("value")
 try:x=float(v);return x if x==x else None
 except:return None
def _price(r,f):
 b=r.get(f) or {}
 if isinstance(b,dict):
  a=[_num(b.get(k)) for k in ("bid","ask","offer","lastTraded")];a=[x for x in a if x is not None];return sum(a)/len(a) if a else None
 return _num(b)
def _candles(p):
 out=[]
 for r in p.get("prices",[]) or []:
  if not isinstance(r,dict):continue
  o,h,l,c=(_price(r,x) for x in ("openPrice","highPrice","lowPrice","closePrice"))
  if None not in (o,h,l,c):out.append({"open":float(o),"high":float(h),"low":float(l),"close":float(c)})
 return out
def structure_signal(m5,m1,spread):
 if len(m5)<24 or len(m1)<8 or spread<=0:return {"eligible":False,"reason":"INSUFFICIENT_CANDLES_OR_SPREAD"}
 br,prior=m5[-2],m5[-22:-2];hi=max(x["high"] for x in prior);lo=min(x["low"] for x in prior);direction=level=None
 if br["close"]>hi:direction,level="BUY",hi
 elif br["close"]<lo:direction,level="SELL",lo
 if not direction:return {"eligible":False,"reason":"NO_M5_STRUCTURE_CLOSE"}
 recent=m1[-7:-1];tol=max(spread*1.5,abs(level)*.00015);retest=any(x["low"]<=level+tol and x["close"]>=level-tol for x in recent) if direction=="BUY" else any(x["high"]>=level-tol and x["close"]<=level+tol for x in recent)
 if not retest:return {"eligible":False,"reason":"NO_RETEST"}
 tr,prev=m1[-2],m1[-3]
 if direction=="BUY":triggered=tr["close"]>prev["high"] and tr["close"]>tr["open"];stop=min(x["low"] for x in recent)-spread*1.25;entry=tr["close"];risk=entry-stop;target=entry+risk*1.5
 else:triggered=tr["close"]<prev["low"] and tr["close"]<tr["open"];stop=max(x["high"] for x in recent)+spread*1.25;entry=tr["close"];risk=stop-entry;target=entry-risk*1.5
 if not triggered or risk<=spread*1.25:return {"eligible":False,"reason":"NO_M1_TRIGGER_OR_INVALID_RISK"}
 return {"eligible":True,"reason":"QUALIFIED","direction":direction,"entry_reference":entry,"structure_level":level,"stop":stop,"target":target,"risk_distance":risk,"target_r":1.5}
def _rule_distance(rule,reference):
 if not isinstance(rule,dict):return None
 value=_num(rule.get("value"));unit=str(rule.get("unit") or "POINTS").upper()
 if value is None or value<0:return None
 return reference*value/100 if "PERCENT" in unit else value
def _safe_name(name,aliases):
 n=str(name or "").upper();return any(a in n for a in aliases if len(a)>=3)
class WeekendMarketEngine:
 def __init__(self,broker):
  self.broker=broker;self.enabled=str(os.getenv("WEEKEND_AUTOTRADE","true")).lower() in {"1","true","yes","on"};self.poll_seconds=max(60,int(os.getenv("WEEKEND_SCAN_SECONDS","180")));self.max_open=max(1,min(2,int(os.getenv("WEEKEND_MAX_OPEN_POSITIONS","2"))));self.max_spread_bps=max(5,float(os.getenv("WEEKEND_MAX_SPREAD_BPS","100")));self.default_size=max(.0001,float(os.getenv("WEEKEND_DEFAULT_SIZE","0.5")));self.friday_start_utc=max(0,min(23,int(os.getenv("WEEKEND_FRIDAY_START_UTC","18"))));self.known_refresh=max(60,int(os.getenv("WEEKEND_KNOWN_MARKET_REFRESH_SECONDS","180")));self.discovery_refresh=max(900,int(os.getenv("WEEKEND_UNSUPPORTED_DISCOVERY_SECONDS","3600")));self._availability={};self._discovery_cache={};self._stop=threading.Event();self._thread=None;self._state={"version":VERSION,"strategy_id":STRATEGY_ID,"enabled":self.enabled,"last_scan_at":None,"last_error":None,"markets":[],"signals":[],"market_discovery":[],"execution_attempts":[],"opens":0}
 def _scan_window(self):
  if str(os.getenv("WEEKEND_FORCE_SCAN","false")).lower() in {"1","true","yes","on"}:return True
  n=datetime.now(timezone.utc);return n.weekday()>=5 or (n.weekday()==4 and n.hour>=self.friday_start_utc)
 def _owned_open(self):return [i for i in (self.broker.positions() or {}).get("positions",[]) or [] if str((i.get("position") or {}).get("dealReference") or "").upper().startswith("JSWKND_")]
 def _snapshot_known(self,seed,known):
  d={"symbol":seed["symbol"],"terms":seed["terms"],"eligible":False,"stage":"KNOWN_MARKET_STATUS","resolver":"KNOWN_EPIC","epic":known["epic"],"resolved_name":known.get("name")}
  try:
   details=self.broker.market_details(known["epic"],require_quote=False);ins,snap=details.get("instrument") or {},details.get("snapshot") or {};q=self.broker.extract_snapshot_quote(details);s={"epic":known["epic"],"instrumentName":ins.get("name") or known.get("name"),"instrumentType":ins.get("type") or known.get("instrument_type"),"category":"CRYPTO","marketStatus":snap.get("marketStatus"),"bid":q.get("bid"),"offer":q.get("offer"),"symbol":seed["symbol"],"expiry":ins.get("expiry") or "-"};v=assess_market(s);d.update({"market_status":s.get("marketStatus"),"instrument_type":s.get("instrumentType"),"bid":s.get("bid"),"offer":s.get("offer"),"eligible":bool(v.get("eligible")),"reason":v.get("reason"),"known_market":True});known.update({"name":s["instrumentName"],"instrument_type":s["instrumentType"],"last_status":s["marketStatus"],"last_checked":time.time()});return (s if v.get("eligible") else None),d
  except Exception as e:d.update({"reason":"KNOWN_MARKET_CHECK_EXCEPTION","error_type":type(e).__name__,"error":str(e)[:300]});return None,d
 def _raw_candidates(self,seed):
  seen={};attempts=[]
  for term in seed["terms"]:
   try:
    rows=(self.broker._request("GET","/markets",version=1,query={"searchTerm":term}).get("markets",[]) or []);safe=[]
    for r in rows:
     name=r.get("instrumentName") or r.get("name") or "";epic=str(r.get("epic") or "");status=str(r.get("marketStatus") or "").upper();item={"term":term,"name":name,"epic":epic,"market_status":status,"instrument_type":r.get("instrumentType")}
     if _safe_name(name,seed["aliases"]):safe.append(item);seen[epic]=item
    attempts.append({"term":term,"returned":len(rows),"safe_matches":safe[:8]})
   except Exception as e:attempts.append({"term":term,"error_type":type(e).__name__,"error":str(e)[:300]})
  candidates=[x for x in seen.values() if x["epic"] and x["market_status"]=="TRADEABLE"];candidates.sort(key=lambda x:(0 if str(x.get("instrument_type") or "").upper()=="CURRENCIES" else 1,len(str(x.get("name") or ""))));return candidates,attempts
 def _resolve(self,seed):
  symbol=seed["symbol"];now=time.time();known=self._availability.get(symbol)
  if known and now-known.get("last_checked",0)<self.known_refresh:return self._snapshot_known(seed,known)
  if known:return self._snapshot_known(seed,known)
  cached=self._discovery_cache.get(symbol)
  if cached and now-cached["at"]<self.discovery_refresh:
   d=dict(cached["diagnostic"]);d.update({"cached":True,"next_discovery_in_seconds":max(0,int(self.discovery_refresh-(now-cached["at"]))) });return None,d
  d={"symbol":symbol,"terms":seed["terms"],"eligible":False,"stage":"SEARCH"};m=None
  try:m=self.broker.resolve_global_market(search_terms=list(seed["terms"]),name_tokens=list(seed["aliases"]),require_tradeable=True,cache_key="WKND_"+symbol);d["resolver"]="GLOBAL_SAFE"
  except Exception as primary:
   candidates,raw=self._raw_candidates(seed);d.update({"resolver":"RAW_SAFE_FALLBACK","primary_error":str(primary)[:300],"raw_search":raw})
   if candidates:c=candidates[0];m={"epic":c["epic"],"name":c["name"],"market_status":c["market_status"],"instrument_type":c.get("instrument_type")};d["fallback_candidates"]=candidates[:8]
   else:d.update({"reason":"NO_SAFE_TRADEABLE_RAW_MATCH"});self._discovery_cache[symbol]={"at":now,"diagnostic":dict(d)};return None,d
  try:
   details=self.broker.market_details(str(m["epic"]),require_quote=False);ins,snap=details.get("instrument") or {},details.get("snapshot") or {};q=self.broker.extract_snapshot_quote(details);s={"epic":m["epic"],"instrumentName":ins.get("name") or m.get("name"),"instrumentType":ins.get("type") or m.get("instrument_type"),"category":"CRYPTO","marketStatus":snap.get("marketStatus") or m.get("market_status"),"bid":q.get("bid"),"offer":q.get("offer"),"symbol":symbol,"expiry":ins.get("expiry") or m.get("expiry") or "-"}
   if not _safe_name(s["instrumentName"],seed["aliases"]):d.update({"stage":"DETAILS","reason":"DETAIL_NAME_MISMATCH"});self._discovery_cache[symbol]={"at":now,"diagnostic":dict(d)};return None,d
   self._availability[symbol]={"epic":s["epic"],"name":s["instrumentName"],"instrument_type":s["instrumentType"],"last_status":s["marketStatus"],"last_checked":now};v=assess_market(s);d.update({"stage":"POLICY","epic":s["epic"],"resolved_name":s["instrumentName"],"market_status":s["marketStatus"],"instrument_type":s["instrumentType"],"bid":s["bid"],"offer":s["offer"],"eligible":bool(v.get("eligible")),"reason":v.get("reason"),"known_market":True});return (s if v.get("eligible") else None),d
  except Exception as e:d.update({"reason":"DETAILS_EXCEPTION","error_type":type(e).__name__,"error":str(e)[:300]});return None,d
 def _signal(self,m):
  bid,offer=float(m["bid"]),float(m["offer"]);spread=offer-bid;mid=(offer+bid)/2;bps=spread/mid*10000 if mid>0 else 999999;g=execution_guard(m,max_spread=mid*self.max_spread_bps/10000)
  if not g.get("eligible"):return {"symbol":m["symbol"],**g}
  return {"symbol":m["symbol"],"epic":m["epic"],"spread_bps":bps,**structure_signal(_candles(self.broker.historical_prices_epic(m["epic"],resolution="MINUTE_5",num_points=40)),_candles(self.broker.historical_prices_epic(m["epic"],resolution="MINUTE",num_points=20)),spread)}
 def _execute(self,sig):
  details=self.broker.market_details(sig["epic"],require_quote=True);snap=details.get("snapshot") or {};q=self.broker.extract_snapshot_quote(details);ins=details.get("instrument") or {};live={"epic":sig["epic"],"category":"CRYPTO","instrumentType":ins.get("type"),"marketStatus":snap.get("marketStatus"),"bid":q.get("bid"),"offer":q.get("offer")}
  if not execution_guard(live).get("eligible"):raise RuntimeError("PRE_ORDER_MARKET_GUARD_FAILED")
  bid,offer=float(q["bid"]),float(q["offer"]);entry=offer if sig["direction"]=="BUY" else bid;rules=details.get("dealingRules") or {};broker_min=max(_rule_distance(rules.get("minNormalStopOrLimitDistance"),entry) or 0,_rule_distance(rules.get("minControlledRiskStopDistance"),entry) or 0,offer-bid);structural_risk=abs(entry-float(sig["stop"]));risk=max(structural_risk,broker_min*1.05);stop=entry-risk if sig["direction"]=="BUY" else entry+risk;target=entry+risk*1.5 if sig["direction"]=="BUY" else entry-risk*1.5;size=self.broker._normalise_deal_size(self.default_size,minimum_size=self.broker._min_deal_size(details),increment=self.broker._deal_size_increment(details));payload={"currencyCode":self.broker._default_currency(ins),"dealReference":("JSWKND_"+uuid.uuid4().hex[:20])[:30],"direction":sig["direction"],"epic":sig["epic"],"expiry":str(ins.get("expiry") or "-"),"forceOpen":True,"guaranteedStop":False,"orderType":"MARKET","size":round(size,12),"stopLevel":round(stop,10),"limitLevel":round(target,10)};ack=self.broker._request("POST","/positions/otc",version=2,payload=payload);ref=str(ack.get("dealReference") or payload["dealReference"]);conf=self.broker.confirm(ref);result={"dealReference":ref,"dealId":conf.get("dealId"),"dealStatus":conf.get("dealStatus"),"direction":sig["direction"],"entry":entry,"stop":stop,"target":target,"target_r":1.5,"structural_risk":structural_risk,"broker_min_distance":broker_min,"risk_adjusted_for_broker":risk>structural_risk}
  if str(conf.get("dealStatus") or "").upper()=="REJECTED":raise RuntimeError("IG_REJECTED: "+str(conf.get("reason") or conf))
  self._state["opens"]=int(self._state.get("opens") or 0)+1;return result
 def tick(self):
  self._state["last_scan_at"]=time.time()
  if not self.enabled or not self._scan_window():self._state.update({"markets":[],"signals":[],"market_discovery":[],"execution_attempts":[]});return self.status()
  try:
   opens=self._owned_open()
   if len(opens)>=self.max_open:return self.status()
   resolved=[self._resolve(s) for s in CRYPTO_SEEDS];markets=[m for m,_ in resolved if m];discovery=[d for _,d in resolved];signals=[self._signal(m) for m in markets];qualified=sorted([s for s in signals if s.get("eligible")],key=lambda x:(float(x.get("spread_bps") or 999999),str(x.get("symbol") or "")));self._state.update({"markets":markets,"market_discovery":discovery,"signals":signals,"execution_attempts":[],"last_error":None});attempts=[];successes=0;slots=self.max_open-len(opens)
   for sig in qualified:
    if successes>=slots:break
    try:r=self._execute(sig);attempts.append({"symbol":sig["symbol"],"status":"OPENED",**r});successes+=1
    except Exception as e:attempts.append({"symbol":sig.get("symbol"),"epic":sig.get("epic"),"status":"REJECTED","error_type":type(e).__name__,"error":str(e)[:500],"direction":sig.get("direction"),"planned_stop":sig.get("stop"),"planned_target":sig.get("target")})
   self._state["execution_attempts"]=attempts;self._state["last_executions"]=[a for a in attempts if a.get("status")=="OPENED"]
  except Exception as e:self._state["last_error"]=f"{type(e).__name__}: {e}"
  return self.status()
 def status(self):
  active=self._scan_window();return {**self._state,"weekend_window":active,"scan_window":active,"friday_start_utc":self.friday_start_utc,"broker_status_is_final_authority":True,"availability_management":{"known_market_refresh_seconds":self.known_refresh,"unsupported_rediscovery_seconds":self.discovery_refresh,"known_markets":self._availability},"demo_only":True,"live_money_execution":False,"max_open_positions":self.max_open,"target_r":1.5}
 def start_thread(self):
  if self._thread and self._thread.is_alive():return
  def loop():
   while not self._stop.is_set():self.tick();self._stop.wait(self.poll_seconds)
  self._thread=threading.Thread(target=loop,name="weekend-market-v613",daemon=True);self._thread.start()
