from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


class RiskGateway:
    """V5.8/V6 broker-agnostic pre-trade risk gateway."""

    def __init__(self, max_open_trades=2, max_daily_loss_pct=0.04,
                 max_drawdown_pct=0.10, max_consecutive_losses=3,
                 max_assumed_spread_bps=3.0, max_price_age_seconds=180):
        self.max_open_trades=int(max_open_trades)
        self.max_daily_loss_pct=float(max_daily_loss_pct)
        self.max_drawdown_pct=float(max_drawdown_pct)
        self.max_consecutive_losses=int(max_consecutive_losses)
        self.max_assumed_spread_bps=float(max_assumed_spread_bps)
        self.max_price_age_seconds=int(max_price_age_seconds)
        self._lock=threading.RLock()
        self._kill_switch=False
        self._kill_reason=None
        self._kill_time=None
        self._last_decision=None

    @staticmethod
    def _safe_float(value: Any, default: float=0.0)->float:
        try:
            number=float(value)
            if math.isnan(number) or math.isinf(number): return default
            return number
        except (TypeError, ValueError): return default

    @staticmethod
    def _forex_market_open(now: Optional[datetime]=None)->bool:
        now=now or datetime.now(timezone.utc)
        weekday=now.weekday(); hour=now.hour
        if weekday==5: return False
        if weekday==6 and hour<22: return False
        if weekday==4 and hour>=22: return False
        return True

    def activate_kill_switch(self, reason: str)->Dict[str,Any]:
        with self._lock:
            self._kill_switch=True; self._kill_reason=str(reason or 'MANUAL_KILL_SWITCH'); self._kill_time=time.time()
        return self.status()

    def clear_kill_switch(self)->Dict[str,Any]:
        with self._lock:
            self._kill_switch=False; self._kill_reason=None; self._kill_time=None
        return self.status()

    def status(self)->Dict[str,Any]:
        with self._lock:
            return {'kill_switch':self._kill_switch,'kill_reason':self._kill_reason,'kill_time':self._kill_time,
                    'limits':{'max_open_trades':self.max_open_trades,'max_daily_loss_pct':self.max_daily_loss_pct,
                    'max_drawdown_pct':self.max_drawdown_pct,'max_consecutive_losses':self.max_consecutive_losses,
                    'max_assumed_spread_bps':self.max_assumed_spread_bps,'max_price_age_seconds':self.max_price_age_seconds},
                    'last_decision':dict(self._last_decision) if self._last_decision else None}

    def evaluate(self, *, symbol:str, direction:str, starting_balance:float, current_balance:float,
                 daily_pnl:float, max_drawdown:float, consecutive_losses:int, open_trades:int,
                 strategy_health:str, live_signal:Dict[str,Any], assumed_spread_bps:float,
                 correlation_conflicts:Optional[Iterable[str]]=None, duplicate_open:bool=False)->Dict[str,Any]:
        blocks=[]; warnings=[]
        with self._lock: kill=self._kill_switch; reason=self._kill_reason
        if kill: blocks.append(f'KILL_SWITCH_ACTIVE:{reason or "UNSPECIFIED"}')
        if not self._forex_market_open(): blocks.append('FX_MARKET_SESSION_CLOSED')
        starting_balance=max(self._safe_float(starting_balance),0.0); current_balance=max(self._safe_float(current_balance),0.0)
        daily_pnl=self._safe_float(daily_pnl); max_drawdown=abs(self._safe_float(max_drawdown))
        consecutive_losses=int(consecutive_losses or 0); open_trades=int(open_trades or 0)
        if starting_balance<=0 or current_balance<=0: blocks.append('INVALID_OR_DEPLETED_BALANCE')
        if starting_balance>0 and daily_pnl<=-(starting_balance*self.max_daily_loss_pct): blocks.append('V6_DAILY_LOSS_LIMIT_REACHED')
        if max_drawdown>=self.max_drawdown_pct: blocks.append('V6_MAX_DRAWDOWN_REACHED')
        if consecutive_losses>=self.max_consecutive_losses: blocks.append('V6_CONSECUTIVE_LOSS_LIMIT_REACHED')
        if open_trades>=self.max_open_trades: blocks.append('V6_MAX_OPEN_TRADES_REACHED')
        if duplicate_open: blocks.append('V6_DUPLICATE_ORDER_BLOCK')
        conflicts=list(correlation_conflicts or [])
        if conflicts: blocks.append('V6_CORRELATED_EXPOSURE_BLOCK')
        health=str(strategy_health or 'PROBATION').upper()
        if health=='QUARANTINED': blocks.append('V6_STRATEGY_QUARANTINED')
        elif health=='DEGRADING': warnings.append('STRATEGY_HEALTH_DEGRADING')
        elif health=='PROBATION': warnings.append('STRATEGY_HEALTH_PROBATION')
        spread=self._safe_float(assumed_spread_bps)
        if spread>self.max_assumed_spread_bps: blocks.append('V6_SPREAD_TOO_WIDE')
        observed=self._safe_float(live_signal.get('observed_at',time.time()),time.time()); age=max(0.0,time.time()-observed)
        if age>self.max_price_age_seconds: blocks.append('V6_STALE_PRICE')
        if self._safe_float(live_signal.get('price'))<=0: blocks.append('V6_INVALID_PRICE')
        result={'allowed':not blocks,'symbol':symbol,'direction':str(direction or '').upper(),'blocks':blocks,'warnings':warnings,
                'strategy_health':health,'assumed_spread_bps':spread,'price_age_seconds':round(age,2),'open_trades':open_trades,
                'consecutive_losses':consecutive_losses,'daily_pnl':round(daily_pnl,2),'max_drawdown':round(max_drawdown,6),
                'current_balance':round(current_balance,2),'checked_at':time.time(),'market_session_open':self._forex_market_open()}
        with self._lock: self._last_decision=dict(result)
        return result
