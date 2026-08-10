from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class ExecutionGateway:
    """V5.9 broker-shaped execution abstraction. LIVE is hard-disabled in V6.0."""
    VALID_MODES={'PAPER','DEMO','LIVE'}
    def __init__(self, mode='PAPER', allow_live_execution=False):
        self._lock=threading.RLock(); self._orders={}; self._idempotency={}; self._mode='PAPER'; self.allow_live_execution=bool(allow_live_execution); self.set_mode(mode)
    def set_mode(self, mode):
        clean=str(mode or 'PAPER').upper()
        if clean not in self.VALID_MODES: raise ValueError('Execution mode must be PAPER, DEMO or LIVE')
        if clean=='LIVE' and not self.allow_live_execution: raise ValueError('LIVE execution is hard-disabled in V6.0. Use PAPER or DEMO.')
        with self._lock: self._mode=clean
        return self.status()
    def status(self):
        with self._lock:
            open_orders=sum(1 for o in self._orders.values() if o.get('status') in {'FILLED','OPEN'} and not o.get('closed',False))
            return {'mode':self._mode,'live_execution':self._mode=='LIVE' and self.allow_live_execution,
                    'live_hard_disabled':not self.allow_live_execution,'orders_total':len(self._orders),'open_orders':open_orders}
    def place_order(self, *, symbol, direction, requested_price, fill_price, stake, idempotency_key, metadata=None):
        direction=str(direction or '').upper()
        if direction not in {'BUY','SELL'}: raise ValueError('Direction must be BUY or SELL')
        if float(stake)<=0: raise ValueError('Stake must be greater than 0')
        with self._lock:
            if idempotency_key in self._idempotency: return dict(self._orders[self._idempotency[idempotency_key]])
            if self._mode=='LIVE' and not self.allow_live_execution: raise RuntimeError('LIVE execution is hard-disabled')
            oid=str(uuid.uuid4()); now=time.time()
            order={'order_id':oid,'idempotency_key':idempotency_key,'mode':self._mode,'symbol':symbol,'direction':direction,
                   'requested_price':float(requested_price),'fill_price':float(fill_price),'slippage_price':round(float(fill_price)-float(requested_price),10),
                   'stake':float(stake),'status':'FILLED','created_at':now,'filled_at':now,'closed':False,'closed_at':None,
                   'exit_requested_price':None,'exit_fill_price':None,'result':None,'pnl':None,'metadata':dict(metadata or {}),'live_execution':False}
            self._orders[oid]=order; self._idempotency[idempotency_key]=oid; return dict(order)
    def close_order(self, *, order_id, requested_exit_price, fill_exit_price, result, pnl):
        with self._lock:
            order=self._orders.get(order_id)
            if order is None: return None
            if order.get('closed'): return dict(order)
            order.update({'status':'CLOSED','closed':True,'closed_at':time.time(),'exit_requested_price':float(requested_exit_price),
                          'exit_fill_price':float(fill_exit_price),'result':str(result or '').upper(),'pnl':round(float(pnl),2)})
            return dict(order)
    def reject_order(self, *, symbol, direction, reasons, metadata=None):
        with self._lock:
            oid=str(uuid.uuid4()); order={'order_id':oid,'mode':self._mode,'symbol':symbol,'direction':str(direction or '').upper(),
                'status':'REJECTED','reasons':list(reasons or []),'created_at':time.time(),'closed':True,'metadata':dict(metadata or {}),'live_execution':False}
            self._orders[oid]=order; return dict(order)
    def get_order(self, order_id):
        with self._lock:
            order=self._orders.get(order_id); return dict(order) if order else None
    def list_orders(self, limit=200):
        with self._lock:
            orders=sorted(self._orders.values(),key=lambda x:float(x.get('created_at',0) or 0),reverse=True)
            return [dict(x) for x in orders[:max(1,min(int(limit),1000))]]
