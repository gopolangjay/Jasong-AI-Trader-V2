from __future__ import annotations

import threading
import time
from typing import Optional


class AutonomousController:
    """V6 supervisory controller. It coordinates existing signal/forward logic; it does not invent signals."""
    LOOP_SECONDS=20
    def __init__(self, *, auto_manager, watcher_engine, risk_gateway, execution_gateway, starting_balance=10000.0):
        self.auto_manager=auto_manager; self.watcher_engine=watcher_engine; self.risk_gateway=risk_gateway; self.execution_gateway=execution_gateway
        self._lock=threading.RLock(); self._stop_event=threading.Event(); self._thread=None
        self._state={'enabled':False,'overnight_mode':False,'starting_balance':float(starting_balance),'started_at':None,'stopped_at':None,
                     'last_heartbeat':None,'heartbeats':0,'last_forward_stats':None,'automatic_shutdown_reason':None,'live_execution':False}
    def start_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive(): return
            self._stop_event.clear(); self._thread=threading.Thread(target=self._loop,name='jasong-v6-controller',daemon=True); self._thread.start()
    def enable(self, *, starting_balance, overnight_mode=True):
        if float(starting_balance)<=0: raise ValueError('starting_balance must be greater than 0')
        self.execution_gateway.set_mode('PAPER'); self.risk_gateway.clear_kill_switch()
        with self._lock:
            self._state.update({'enabled':True,'overnight_mode':bool(overnight_mode),'starting_balance':float(starting_balance),'started_at':time.time(),
                                'stopped_at':None,'automatic_shutdown_reason':None})
        return self.status()
    def disable(self, reason='MANUAL_STOP'):
        try: self.auto_manager.disable()
        except Exception: pass
        with self._lock:
            self._state.update({'enabled':False,'overnight_mode':False,'stopped_at':time.time(),'automatic_shutdown_reason':reason})
        return self.status()
    def emergency_stop(self, reason='MANUAL_EMERGENCY_STOP'):
        self.risk_gateway.activate_kill_switch(reason); return self.disable(reason)
    def status(self):
        with self._lock: state=dict(self._state)
        state['risk_gateway']=self.risk_gateway.status(); state['execution_gateway']=self.execution_gateway.status(); state['auto_manager']=self.auto_manager.status(); return state
    def report(self):
        bal=float(self._state.get('starting_balance',10000.0) or 10000.0); forward=self.watcher_engine.forward_stats(starting_balance=bal)
        watchers=self.watcher_engine.list(); orders=self.execution_gateway.list_orders(limit=500)
        accepted=[x for x in orders if x.get('status') in {'FILLED','OPEN','CLOSED'}]; rejected=[x for x in orders if x.get('status')=='REJECTED']
        return {'version':'6.0.0','controller':self.status(),'forward':forward,'watchers_total':len(watchers),'watchers':watchers[:50],
                'execution':{'orders_total':len(orders),'accepted_orders':len(accepted),'rejected_orders':len(rejected),'orders':orders[:100]},'live_execution':False}
    def _safety(self, forward):
        if not self._state.get('enabled'): return None
        start=float(self._state.get('starting_balance',10000.0) or 10000.0); pnl=float(forward.get('total_pnl',0.0) or 0.0); dd=abs(float(forward.get('max_drawdown',0.0) or 0.0))
        if start>0 and pnl<=-(start*self.risk_gateway.max_daily_loss_pct): return 'V6_AUTOMATIC_DAILY_LOSS_SHUTDOWN'
        if dd>=self.risk_gateway.max_drawdown_pct: return 'V6_AUTOMATIC_DRAWDOWN_SHUTDOWN'
        return None
    def _heartbeat(self):
        bal=float(self._state.get('starting_balance',10000.0) or 10000.0); forward=self.watcher_engine.forward_stats(starting_balance=bal); reason=self._safety(forward)
        if reason: self.emergency_stop(reason)
        with self._lock:
            self._state['last_heartbeat']=time.time(); self._state['heartbeats']=int(self._state.get('heartbeats',0))+1; self._state['last_forward_stats']=forward
    def _loop(self):
        while not self._stop_event.is_set():
            try:
                if self._state.get('enabled'): self._heartbeat()
            except Exception: pass
            self._stop_event.wait(self.LOOP_SECONDS)
