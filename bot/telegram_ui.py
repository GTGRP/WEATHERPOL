"""
Telegram Bot Integration — Notifications + Commands.

Features:
- Send trade alerts (entry, exit, win, loss)
- Detailed redemption alerts (full market, entry/exit, profit/PnL)
- ONE grouped "Peak Cluster Box N" alert per basket (not one per leg)
- Send daily summary
- Commands: /status, /positions, /balance, /pnl, /markets, /stop
- Paginated + sortable positions view (10 per page), peak-cluster legs grouped
- Non-blocking (runs in background thread)
"""

import os
import json
import html
import time
import threading
import requests
from typing import Optional, Dict, List
from datetime import datetime, timezone

from config import Config
from logger import log


class TelegramBot:
    """Telegram bot for notifications and commands."""

    PAGE_SIZE = 10
    _SORT_NAMES = {
        'pnl': 'Top PnL', 'loss': 'Biggest losers',
        'roi': 'Top ROI', 'recent': 'Most recent',
    }

    def __init__(self, position_manager=None, scanner=None):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.pm = position_manager
        self.scanner = scanner
        self._session = requests.Session()
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_id = 0
        # Optional dashboard hook: restart_fresh(starting_balance=None) clears
        # ALL positions and resets the paper balance for a fresh start. Set by
        # the dashboard; the inline Restart button / /restart invoke it.
        self._on_restart = None
        self._restart_pending = False
        # Seed with already-redeemed ids so a restart doesn't re-announce the
        # whole backlog — only NEW redemptions after startup are sent.
        self._announced_redeemed = set(
            p.id for p in self.pm.positions if p.status == 'redeemed'
        ) if self.pm else set()

        if not self.enabled:
            log.info("Telegram: disabled (no token/chat_id set)")
        else:
            log.info(f"Telegram: enabled → chat {self.chat_id}")

    @property
    def base_url(self):
        return "https" + "://api.telegram.org/bot" + str(self.token)

    @staticmethod
    def _esc(s) -> str:
        """HTML-escape dynamic text so market names with &/</> don't break parse."""
        return html.escape(str(s if s is not None else ''))

    # ==============================================================
    # SEND MESSAGES
    # ==============================================================

    def send(self, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> bool:
        """Send a message to the configured chat (optionally with an inline keyboard)."""
        if not self.enabled:
            return False
        # Intercept the legacy blind "Redeemed N winning positions!" message and
        # replace it with the detailed per-position breakdown (full market, entry/
        # exit, cost/payout, PnL). The detailed header starts with "<b>REDEEMED",
        # so it never matches this guard and there is no recursion.
        stripped = text.strip()
        if stripped.startswith('\U0001F4B0 Redeemed ') and stripped.endswith('positions!'):
            self.notify_redeems_recent()
            return True
        try:
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            resp = self._session.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram send failed: {e}")
            return False

    def _edit(self, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message (used to refresh panels in place)."""
        if not self.enabled:
            return False
        try:
            payload = {
                'chat_id': self.chat_id, 'message_id': message_id,
                'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            r = self._session.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.debug(f"Telegram edit failed: {e}")
            return False

    def _answer_callback(self, callback_id: str, text: str = ''):
        try:
            self._session.post(f"{self.base_url}/answerCallbackQuery",
                               json={'callback_query_id': callback_id, 'text': text},
                               timeout=10)
        except Exception:
            pass

    # ==============================================================
    # LIFECYCLE (startup ready / start / restart fresh)
    # ==============================================================

    def _main_keyboard(self) -> dict:
        """Inline keyboard shown on startup: Start / Settings / Restart."""
        return {'inline_keyboard': [[
            {'text': '▶️ Start Trading', 'callback_data': 'act:start'},
            {'text': '⚙️ Settings', 'callback_data': 'act:settings'},
            {'text': '♻️ Restart', 'callback_data': 'act:restart'},
        ]]}

    def send_startup_ready(self):
        """Announce a successful deploy/boot WITHOUT auto-trading and show the
        Start / Settings / Restart inline keyboard. Trading begins only when the
        user taps Start Trading (or sends /start, or types 'start')."""
        try:
            from bot import settings_store
            bools, _nums = settings_store.snapshot()
        except Exception:
            bools = {}
        try:
            bal = self.pm.get_balance() if self.pm else 0.0
        except Exception:
            bal = 0.0
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        trading = '🟢 ON' if bools.get('TRADING_ENABLED') else '🔴 OFF (tap Start Trading)'
        msg = (
            f"✅ <b>Bot initialized successfully</b>\n"
            f"{mode} | starting balance ${bal:.2f}\n"
            f"Trading: <b>{trading}</b>\n\n"
            f"▶️ <b>Start Trading</b> — begin placing trades (or send /start)\n"
            f"⚙️ <b>Settings</b> — strategies, gates & starting balance\n"
            f"♻️ <b>Restart</b> — clear ALL positions & start fresh\n"
        )
        self.send(msg, reply_markup=self._main_keyboard())

    def _prompt_restart(self):
        """Ask for confirmation before the destructive restart-fresh action."""
        self._restart_pending = True
        kb = {'inline_keyboard': [[
            {'text': '✅ Yes, clear all & restart', 'callback_data': 'act:restart_confirm'},
            {'text': '✖️ Cancel', 'callback_data': 'act:restart_cancel'},
        ]]}
        self.send(
            "♻️ <b>Restart fresh?</b>\n"
            "This CLOSES/clears ALL positions and resets the paper balance to "
            "the configured starting balance. This cannot be undone.",
            reply_markup=kb,
        )

    def _do_restart(self):
        """Invoke the dashboard restart hook (clear all positions + reset balance)."""
        self._restart_pending = False
        if not self._on_restart:
            self.send("⚠️ Restart hook not wired — cannot restart from here.")
            return
        try:
            self._on_restart()
            try:
                bal = self.pm.get_balance() if self.pm else 0.0
            except Exception:
                bal = 0.0
            self.send(
                f"♻️ <b>Restarted fresh</b> — all positions cleared, "
                f"balance reset to ${bal:.2f}. Tap Start Trading to begin.",
                reply_markup=self._main_keyboard(),
            )
        except Exception as e:
            log.debug(f"restart failed: {e}")
            self.send("⚠️ Restart failed — see logs.")

    def notify_trade(self, side: str, bucket_label: str, price: float,
                     size_usd: float, shares: float, strategy: str,
                     edge: float = 0, city: str = ''):
        """Send trade notification."""
        emoji = '🟢' if side == 'BUY' else '🔴'
        msg = (
            f"{emoji} <b>{side}</b> — {self._esc(strategy.upper())}\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 ${price:.4f} × {shares:.0f} = ${size_usd:.2f}\n"
        )
        if edge > 0:
            msg += f"📊 Edge: {edge:.1%}\n"
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        msg += f"\n{mode}"
        self.send(msg)

    def notify_cluster(self, box_label: str, city: str, market_title: str,
                       legs: List, total_cost: float, combined_prob: float,
                       roi_pct: float, group_label: str = None):
        """ONE grouped alert for a whole peak-cluster basket.

        Replaces the old behaviour of firing a separate notify_trade per leg
        (6 buckets => 6 messages). Now a single "🧺 PEAK CLUSTER Box N" message
        lists every bucket bought, the combined basket cost, and the any-one-
        wins ROI. `legs` is the list of placed TrackedPositions in the basket.
        """
        try:
            n = len(legs)
            total_cost_usd = sum(getattr(l, 'cost_usd', 0.0) or 0.0 for l in legs)
            title = (group_label or 'PEAK CLUSTER').upper()
            head = (
                f"🧺 <b>{self._esc(title)} {self._esc(box_label)}</b> — {n} bucket{'s' if n != 1 else ''}\n"
                f"📍 {self._esc(city)} | {self._esc((market_title or '')[:60])}\n"
            )
            lines = []
            for l in legs:
                lines.append(
                    f"   • {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"@ ${getattr(l, 'entry_price', 0.0):.3f} × "
                    f"{getattr(l, 'shares', 0.0):.0f} = ${getattr(l, 'cost_usd', 0.0):.2f}\n"
                )
            foot = (
                f"💰 basket cost ${total_cost_usd:.2f} "
                f"(per-share ${total_cost:.3f}) | P(any)~{combined_prob:.0%}\n"
                f"🎯 ROI ~{roi_pct:.0f}% if ANY bucket wins | holds → resolution "
                f"(never stop-lossed)\n"
            )
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            self.send(head + ''.join(lines) + foot + f"\n{mode}")
        except Exception as e:
            log.debug(f"notify_cluster failed: {e}")

    def notify_cluster_resolution(self, box_label: str, legs: List):
        """ONE grouped resolution summary for a peak-cluster basket once EVERY
        leg has settled. Shows which bucket WON and the amount it won, plus the
        losing buckets and their loss, and the net basket PnL. Replaces the
        per-leg won/lost spam for cluster baskets.

        `legs` is the list of resolved TrackedPositions in the basket (fed by
        PositionManager._maybe_notify_cluster_close once none are open/pending).
        """
        try:
            if not legs:
                return
            city = self._esc(getattr(legs[0], 'city', ''))
            market_title = self._esc((getattr(legs[0], 'market_title', '') or '')[:60])
            winners = [l for l in legs if getattr(l, 'status', '') in ('won', 'redeemed')]
            losers = [l for l in legs if getattr(l, 'status', '') == 'lost']
            others = [l for l in legs if l not in winners and l not in losers]
            net = sum(getattr(l, 'pnl', 0.0) or 0.0 for l in legs)
            cost = sum(getattr(l, 'cost_usd', 0.0) or 0.0 for l in legs)
            ret = cost + net
            head_emoji = '✅' if net >= 0 else '🔴'
            head = (
                f"{head_emoji} 🧺 <b>PEAK CLUSTER {self._esc(box_label)} RESOLVED</b>\n"
                f"📍 {city} | {market_title}\n"
            )
            lines = []
            if winners:
                for l in winners:
                    payout = (getattr(l, 'shares', 0.0) or 0.0) * 1.0
                    lines.append(
                        f"   ✅ WON {self._esc(getattr(l, 'bucket_label', ''))} "
                        f"→ ${getattr(l, 'pnl', 0.0):+.2f} "
                        f"(entry ${getattr(l, 'entry_price', 0.0):.3f} × "
                        f"{getattr(l, 'shares', 0.0):.0f}sh → payout ${payout:.2f})\n"
                    )
            else:
                lines.append("   ⚠️ No winning bucket in this basket.\n")
            for l in losers:
                lines.append(
                    f"   ❌ {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"→ ${getattr(l, 'pnl', 0.0):+.2f} "
                    f"(cost ${getattr(l, 'cost_usd', 0.0):.2f} lost)\n"
                )
            for l in others:
                lines.append(
                    f"   • {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"→ ${getattr(l, 'pnl', 0.0):+.2f} ({self._esc(getattr(l, 'status', ''))})\n"
                )
            foot = (
                f"💰 <b>Basket net PnL ${net:+.2f}</b> "
                f"(cost ${cost:.2f} → return ${ret:.2f})\n"
            )
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            self.send(head + ''.join(lines) + foot + f"\n{mode}")
        except Exception as e:
            log.debug(f"notify_cluster_resolution failed: {e}")

    def notify_resolution(self, won: bool, bucket_label: str, pnl: float, city: str = ''):
        """Send simple resolution notification (kept for compatibility)."""
        emoji = '✅' if won else '❌'
        result = 'WON' if won else 'LOST'
        msg = (
            f"{emoji} <b>RESOLVED: {result}</b>\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 PnL: ${pnl:+.2f}\n"
        )
        self.send(msg)

    def notify_close(self, pos):
        """Send a close/resolution alert for ANY closed position — stop-loss,
        take-profit, trailing-stop, flip/thesis exit, or won/lost resolution.

        Wired via PositionManager._notify_close (risk-trigger & resolution
        closes) and called directly by the dashboard for flip/thesis exits
        (whose reason is relabeled 'manual' after close, so the PM hook skips
        them to avoid a double-notify). Fully defensive — never raises."""
        try:
            reason = getattr(pos, 'exit_reason', '') or ''
            status = getattr(pos, 'status', '') or ''
            pnl = getattr(pos, 'pnl', 0.0) or 0.0
            roi = getattr(pos, 'roi_pct', 0.0) or 0.0
            if status == 'won':
                head = '✅ <b>RESOLVED WON</b>'
            elif status == 'lost':
                head = '❌ <b>RESOLVED LOST</b>'
            else:
                head = {
                    'take_profit': '🎯 <b>TAKE PROFIT</b>',
                    'stop_loss': '🛑 <b>STOP LOSS</b>',
                    'trailing_stop': '📉 <b>TRAILING STOP</b>',
                    'flip_timeout': '⏲️ <b>FLIP book-or-cut</b>',
                    'thesis_invalidated': '🚫 <b>THESIS EXIT</b>',
                    'manual': '🔴 <b>SOLD</b>',
                }.get(reason, '🔴 <b>SOLD</b>')
            entry = getattr(pos, 'entry_price', 0.0) or 0.0
            exit_px = getattr(pos, 'exit_price', None)
            if exit_px is None:
                exit_px = getattr(pos, 'current_price', 0.0) or 0.0
            shares = getattr(pos, 'shares', 0.0) or 0.0
            name = self._esc(getattr(pos, 'bucket_label', '') or getattr(pos, 'market_title', ''))
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            msg = (
                f"{head} — {self._esc(getattr(pos, 'strategy', ''))}\n"
                f"📍 {self._esc(getattr(pos, 'city', ''))} | {name}\n"
                f"💵 entry ${entry:.4f} → exit ${exit_px:.4f} | {shares:.0f}sh\n"
                f"📊 PnL ${pnl:+.2f} ({roi:+.0f}%)\n"
                f"{mode}"
            )
            self.send(msg)
        except Exception as e:
            log.debug(f"notify_close failed: {e}")

    def notify_redeems_recent(self):
        """Find positions that have newly become 'redeemed' since the last call
        and announce them in full detail. Self-discovers from the position
        manager so the dashboard doesn't need to pass anything."""
        if not self.pm:
            return
        fresh = [p for p in self.pm.positions
                 if p.status == 'redeemed' and p.id not in self._announced_redeemed]
        for p in fresh:
            self._announced_redeemed.add(p.id)
        if fresh:
            self.notify_redeems(fresh)

    def notify_redeems(self, positions: List):
        """Detailed redemption notification — one block per redeemed position with
        the full market name, entry/exit price, cost/payout and realized PnL."""
        if not positions:
            return
        payout_total = sum(p.shares * 1.0 for p in positions)
        pnl_total = sum(p.pnl for p in positions)
        n = len(positions)
        header = (
            f"💰 <b>REDEEMED {n} winning position{'s' if n != 1 else ''}</b>\n"
            f"   payout +${payout_total:.2f} | realized PnL ${pnl_total:+.2f}\n"
        )
        blocks = []
        for p in positions:
            exit_px = p.exit_price if p.exit_price is not None else 1.0
            payout = p.shares * 1.0
            name = self._esc(p.bucket_label or p.market_title)
            blocks.append(
                f"\n✅ <b>{self._esc(p.city)}</b>  ({self._esc(p.strategy)})\n"
                f"   {name}\n"
                f"   entry ${p.entry_price:.4f} → exit ${exit_px:.4f} | {p.shares:.0f}sh\n"
                f"   cost ${p.cost_usd:.2f} → payout ${payout:.2f} | "
                f"PnL ${p.pnl:+.2f} ({p.roi_pct:+.0f}%)\n"
            )
        # Respect Telegram's ~4096-char message cap — chunk if necessary.
        msg = header
        for b in blocks:
            if len(msg) + len(b) > 3900:
                self.send(msg)
                msg = ''
            msg += b
        if msg:
            self.send(msg)

    def notify_redeem(self, bucket_label: str, amount: float):
        """Legacy single-redeem notification (kept for compatibility)."""
        msg = (
            f"💰 <b>REDEEMED</b>\n"
            f"📍 {self._esc(bucket_label)}\n"
            f"💵 +${amount:.2f}\n"
        )
        self.send(msg)

    # ==============================================================
    # POSITIONS VIEW (paginated + sortable, peak-cluster legs GROUPED)
    # ==============================================================

    def _open_units(self, sort_key: str) -> List[dict]:
        """Group open positions into display UNITS so a peak-cluster basket shows
        as ONE entry ("Box N" + all its legs) instead of N separate rows.

        Each unit: {kind, box, positions, pnl, roi, recent}. Non-cluster
        positions are single-position units. Units are sorted as a whole.
        """
        open_pos = self.pm.get_open_positions() if self.pm else []
        clusters: Dict[str, list] = {}
        units: List[dict] = []
        basket_strats = ('peak_cluster', 'peaker_cool_basket', 'peaker_warm_basket')
        for p in open_pos:
            box = getattr(p, 'cluster_box', '') or ''
            if box and getattr(p, 'strategy', '') in basket_strats:
                clusters.setdefault(box, []).append(p)
            else:
                units.append({
                    'kind': 'single', 'box': '', 'positions': [p],
                    'pnl': p.unrealized_pnl, 'roi': p.roi_pct,
                    'recent': p.entry_time,
                })
        for box, legs in clusters.items():
            pnl = sum(l.unrealized_pnl for l in legs)
            cost = sum(l.cost_usd for l in legs)
            roi = (pnl / cost * 100.0) if cost > 0 else 0.0
            recent = max(l.entry_time for l in legs)
            strat = getattr(legs[0], 'strategy', 'peak_cluster') if legs else 'peak_cluster'
            units.append({
                'kind': 'cluster', 'box': box, 'positions': legs,
                'pnl': pnl, 'roi': roi, 'recent': recent, 'strategy': strat,
            })
        if sort_key == 'pnl':
            units.sort(key=lambda u: u['pnl'], reverse=True)
        elif sort_key == 'loss':
            units.sort(key=lambda u: u['pnl'])
        elif sort_key == 'roi':
            units.sort(key=lambda u: u['roi'], reverse=True)
        else:  # 'recent'
            units.sort(key=lambda u: u['recent'], reverse=True)
        return units

    def _fmt_position(self, p, idx: int) -> str:
        pe = '🟢' if p.unrealized_pnl >= 0 else '🔴'
        lock = ' 🔒' if getattr(p, 'preclose_locked', False) else ''
        stale = ' ~stale' if getattr(p, 'current_price_stale', False) else ''
        name = self._esc(p.bucket_label or p.market_title)
        return (
            f"{idx}. {pe} <b>{self._esc(p.city)}</b>  "
            f"${p.unrealized_pnl:+.2f} ({p.roi_pct:+.0f}%){lock}{stale}\n"
            f"   {name}\n"
            f"   entry ${p.entry_price:.4f} → ${p.current_price:.4f} | "
            f"{p.shares:.0f}sh | cost ${p.cost_usd:.2f} | {self._esc(p.strategy)}\n\n"
        )

    def _fmt_cluster_unit(self, unit: dict, idx: int) -> str:
        """Render a whole peak-cluster basket as ONE grouped block: a "Box N"
        header with the aggregate PnL, then each bucket leg indented under it."""
        legs = unit['positions']
        pe = '🟢' if unit['pnl'] >= 0 else '🔴'
        city = self._esc(getattr(legs[0], 'city', '') if legs else '')
        cost = sum(l.cost_usd for l in legs)
        label = {
            'peak_cluster': 'Peak Cluster',
            'peaker_cool_basket': 'Peaker Cool Basket',
            'peaker_warm_basket': 'Peaker Warm Basket',
        }.get(unit.get('strategy', 'peak_cluster'), 'Peak Cluster')
        out = (
            f"{idx}. {pe} 🧺 <b>{self._esc(label)} {self._esc(unit['box'])}</b> — {city}  "
            f"${unit['pnl']:+.2f} ({unit['roi']:+.0f}%)\n"
            f"   {len(legs)} buckets | cost ${cost:.2f} | hold → resolution\n"
        )
        for l in legs:
            name = self._esc(l.bucket_label or l.market_title)
            out += (
                f"      • {name}: ${l.entry_price:.3f}→${l.current_price:.3f} "
                f"{l.shares:.0f}sh (${l.unrealized_pnl:+.2f})\n"
            )
        out += "\n"
        return out

    def _positions_view(self, page: int = 0, sort: str = 'recent',
                        with_summary: bool = False):
        """Build (text, inline_keyboard) for a page of open positions.

        Pagination is by display UNIT (a peak-cluster basket counts as one
        unit), so a 6-leg basket no longer eats 6 of the 10 page slots.
        """
        units = self._open_units(sort)
        total_units = len(units)
        total_pos = sum(len(u['positions']) for u in units)
        pages = max(1, (total_units + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * self.PAGE_SIZE
        chunk = units[start:start + self.PAGE_SIZE]

        text = ''
        if with_summary and self.pm:
            s = self.pm.get_stats()
            text += (
                f"📊 <b>Weather Sniper Status</b>\n"
                f"Mode: {s['mode']} | Balance: ${s['balance']:.2f}\n"
                f"PnL: ${s['total_pnl']:+.2f} ({s['roi_pct']:+.1f}%) | "
                f"WR: {s['win_rate']:.0f}% ({s['wins']}W/{s['losses']}L)\n"
                f"Trades: {s['total_trades']} | Open: {s['open_positions']} | "
                f"Redeemed: ${s['total_redeemed']:.2f}\n"
                f"{'-'*28}\n"
            )
        sort_name = self._SORT_NAMES.get(sort, sort)
        shown_to = start + len(chunk)
        text += (f"<b>Open {start + 1}-{shown_to} of {total_units} "
                 f"({total_pos} positions)</b> · sorted: {sort_name}\n\n")
        if not chunk:
            text += "No open positions.\n"
        else:
            for i, u in enumerate(chunk, start=start + 1):
                if u['kind'] == 'cluster':
                    text += self._fmt_cluster_unit(u, i)
                else:
                    text += self._fmt_position(u['positions'][0], i)

        sm = '1' if with_summary else '0'
        nav = []
        if page > 0:
            nav.append({'text': '⬅️ Prev', 'callback_data': f"pos:{page-1}:{sort}:{sm}"})
        nav.append({'text': f"{page+1}/{pages}", 'callback_data': 'noop'})
        if page < pages - 1:
            nav.append({'text': 'Next ➡️', 'callback_data': f"pos:{page+1}:{sort}:{sm}"})
        dot = lambda k: ('• ' if sort == k else '')
        sort_row = [
            {'text': dot('pnl') + '💰 PnL', 'callback_data': f"pos:0:pnl:{sm}"},
            {'text': dot('loss') + '📉 Losses', 'callback_data': f"pos:0:loss:{sm}"},
            {'text': dot('roi') + '📈 ROI', 'callback_data': f"pos:0:roi:{sm}"},
            {'text': dot('recent') + '🕒 Recent', 'callback_data': f"pos:0:recent:{sm}"},
        ]
        return text, {'inline_keyboard': [nav, sort_row]}

    def send_positions(self, page: int = 0, sort: str = 'recent',
                       with_summary: bool = False, edit_message_id: int = None):
        if not self.pm:
            return
        text, kb = self._positions_view(page, sort, with_summary)
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def send_status(self):
        """Status = summary + first page of open positions (paginated/sortable)."""
        if not self.pm:
            return
        self.send_positions(page=0, sort='recent', with_summary=True)

    def send_markets_summary(self):
        """Send summary of available markets."""
        if not self.scanner:
            return
        markets = self.scanner.scan_weather_markets(days_ahead=2)
        msg = f"🌤️ <b>Active Weather Markets: {len(markets)}</b>\n\n"
        by_city: Dict[str, int] = {}
        for m in markets:
            by_city[m.city] = by_city.get(m.city, 0) + 1
        for city, count in sorted(by_city.items(), key=lambda x: -x[1]):
            msg += f"  📍 {self._esc(city)}: {count} markets\n"
        self.send(msg)

    def send_daily_summary(self):
        """Send end-of-day summary."""
        if not self.pm:
            return
        stats = self.pm.get_stats()
        today_positions = [p for p in self.pm.positions
                          if p.entry_time.date() == datetime.now(timezone.utc).date()]
        today_pnl = sum(p.pnl for p in today_positions if p.status != 'open')
        msg = (
            f"📅 <b>Daily Summary</b>\n"
            f"{'-'*30}\n"
            f"New trades today: {len(today_positions)}\n"
            f"Today's PnL: ${today_pnl:+.2f}\n"
            f"Total PnL: ${stats['total_pnl']:+.2f}\n"
            f"Balance: ${stats['balance']:.2f}\n"
            f"Portfolio: ${stats['portfolio_value']:.2f}\n"
            f"Win Rate: {stats['win_rate']:.0f}%\n"
        )
        self.send(msg)

    # ==============================================================
    # ANALYSIS (/analysis) — per-strategy performance + downloadable trade log
    # ==============================================================

    def _trade_log_path(self) -> str:
        """Resolve the paper-trade JSONL path (PositionManager's, else Config)."""
        path = getattr(self.pm, '_paper_trades_file', None) if self.pm else None
        return path or getattr(Config, 'PAPER_TRADE_LOG', 'data/paper_trades.jsonl')

    def _read_trade_log(self) -> List[dict]:
        """Read every structured record from data/paper_trades.jsonl (one per
        BUY / SELL / SETTLE / REDEEM / PRECLOSE_LOCK). Returns [] if missing."""
        path = self._trade_log_path()
        recs: List[dict] = []
        try:
            if not os.path.exists(path):
                return recs
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            log.debug(f"trade log read failed: {e}")
        return recs

    def _send_document(self, file_path: str, caption: str = '') -> bool:
        """Upload a file to the chat as a downloadable document (sendDocument).
        Used by /analysis to ship the raw trade log. Fully defensive."""
        if not self.enabled:
            return False
        try:
            if not os.path.exists(file_path):
                self.send(f"⚠️ Log file not found: {self._esc(file_path)}")
                return False
            with open(file_path, 'rb') as fh:
                files = {'document': (os.path.basename(file_path), fh)}
                data = {'chat_id': self.chat_id}
                if caption:
                    data['caption'] = caption[:1000]
                resp = self._session.post(
                    f"{self.base_url}/sendDocument",
                    data=data, files=files, timeout=30,
                )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram sendDocument failed: {e}")
            return False

    def send_analysis(self):
        """/analysis — full strategy performance breakdown + a downloadable log
        of every BUY / SELL / SETTLE / REDEEM / exit.

        Live W/L/PnL come from the PositionManager (thesis exits ARE counted as
        losses — see PositionManager._closed_outcome), and per-strategy BUY
        counts + action/exit tallies come from data/paper_trades.jsonl.
        """
        if not self.pm:
            self.send("⚠️ Analysis unavailable — position manager not wired.")
            return
        stats = self.pm.get_stats()
        by_strat = self.pm.get_per_strategy_stats()
        recs = self._read_trade_log()

        # Tally actions + per-strategy BUY counts + exit reasons from the log.
        action_counts: Dict[str, int] = {}
        buys_by_strat: Dict[str, int] = {}
        exit_counts: Dict[str, int] = {}
        for r in recs:
            a = r.get('action', '') or '?'
            action_counts[a] = action_counts.get(a, 0) + 1
            if a == 'BUY':
                s = r.get('strategy', '?') or '?'
                buys_by_strat[s] = buys_by_strat.get(s, 0) + 1
            if a in ('SELL', 'SETTLE'):
                xr = r.get('exit_reason', '') or '—'
                exit_counts[xr] = exit_counts.get(xr, 0) + 1

        text = (
            f"📈 <b>Strategy Analysis</b> — {stats['mode']}\n"
            f"Balance ${stats['balance']:.2f} | PnL ${stats['total_pnl']:+.2f} "
            f"({stats['roi_pct']:+.1f}%)\n"
            f"WR {stats['win_rate']:.0f}% ({stats['wins']}W/{stats['losses']}L) | "
            f"Trades {stats['total_trades']} | Open {stats['open_positions']} | "
            f"Redeemed ${stats['total_redeemed']:.2f}\n"
            f"{'-'*28}\n"
            f"<b>By strategy</b> (buys · W/L · WR · PnL)\n"
        )
        if not by_strat:
            text += "  (no trades yet)\n"
        else:
            for strat, s in sorted(by_strat.items(),
                                   key=lambda kv: kv[1]['pnl'], reverse=True):
                closed = s['wins'] + s['losses']
                wr = (s['wins'] / closed * 100.0) if closed else 0.0
                buys = buys_by_strat.get(strat, s['trades'])
                pe = '🟢' if s['pnl'] >= 0 else '🔴'
                text += (
                    f"{pe} <b>{self._esc(strat)}</b>: {buys} buys · "
                    f"{s['wins']}W/{s['losses']}L · {wr:.0f}% · ${s['pnl']:+.2f}\n"
                )

        if action_counts:
            text += f"{'-'*28}\n<b>Log actions</b>: "
            text += " · ".join(f"{self._esc(k)} {v}"
                                for k, v in sorted(action_counts.items()))
            text += "\n"
        if exit_counts:
            text += "<b>Exits</b>: "
            text += " · ".join(f"{self._esc(k)} {v}" for k, v in
                                sorted(exit_counts.items(), key=lambda kv: -kv[1]))
            text += "\n"

        self.send(text)

        # Attach the full machine-readable log as a downloadable document.
        if recs:
            self._send_document(
                self._trade_log_path(),
                caption=(f"📎 Full trade log — {len(recs)} records "
                         f"(BUY/SELL/SETTLE/REDEEM/exits)"),
            )
        else:
            self.send("ℹ️ No trade-log records yet — the log is empty.")

    # ==============================================================
    # COMMAND HANDLER (polls for incoming commands)
    # ==============================================================

    def start_polling(self):
        """Start polling for commands in a background thread."""
        if not self.enabled:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info("Telegram command polling started")

    def stop_polling(self):
        """Stop polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)

    def _poll_loop(self):
        """Background loop to check for incoming commands."""
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                log.debug(f"Telegram poll error: {e}")
            try:
                self.notify_redeems_recent()
            except Exception as e:
                log.debug(f"redeem announce error: {e}")
            time.sleep(3)

    def _check_updates(self):
        """Check for new Telegram messages/commands."""
        try:
            resp = self._session.get(
                f"{self.base_url}/getUpdates",
                params={'offset': self._last_update_id + 1, 'timeout': 2},
                timeout=10,
            )
            if resp.status_code != 200:
                return

            data = resp.json()
            for update in data.get('result', []):
                self._last_update_id = update['update_id']

                cb = update.get('callback_query')
                if cb:
                    cb_chat = str(cb.get('message', {}).get('chat', {}).get('id', ''))
                    if cb_chat == self.chat_id:
                        self._handle_callback(
                            cb.get('data', ''), cb.get('id', ''),
                            cb.get('message', {}).get('message_id'),
                        )
                    continue

                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = str(msg.get('chat', {}).get('id', ''))

                if chat_id != self.chat_id:
                    continue

                self._handle_command(text)
        except Exception:
            pass

    # ==============================================================
    # SETTINGS PANEL (live tunables + tick-box toggles)
    # ==============================================================

    _SETTINGS_DEFAULT_GROUP = 'main'

    # Short button labels for the on/off toggles (fallback = the key name).
    _LABELS = {
        'TRADING_ENABLED': 'Trading',
        'LATE_OBSERVED_ENABLED': 'Late-Obs',
        'LATE_OBSERVED_NO_SIDE': 'LateObs NO',
        'QUICK_FLIP_ENABLED': 'Quick-Flip',
        'PEAK_CLUSTER_ENABLED': 'Cluster',
        'PEAKER_ENABLED': 'Peaker',
        'CONFIDENT_ENABLED': 'Confident',
        'SNIPER_ENABLED': 'Sniper',
        'SPREAD_ENABLED': 'Spread',
        'STABILITY_ENABLED': 'Stability',
        'ML_ENABLED': 'ML',
        'ML_DECISION_ENABLED': 'ML-Decide',
        'AUTO_REDEEM_ENABLED': 'Auto-Redeem',
        'PORTFOLIO_GUARD_ENABLED': 'Port-Guard',
        'QUICK_FLIP_PROFIT_ONLY_EXIT': 'Flip profit-only',
        'QUICK_FLIP_USE_ML_EXIT': 'Flip ML-exit',
        'PEAKER_PREFER_COOL': 'Prefer cool',
        'PEAKER_TRADE_DECIDED': 'Peaker decided',
        'PEAK_CLUSTER_TRADE_DECIDED': 'Cluster decided',
        'THESIS_EXIT_ENABLED': 'Thesis-exit',
        'LIQUIDITY_GUARD_ENABLED': 'LiqGuard',
        'LIQUIDITY_STRICT_BLOCK': 'LiqStrict',
        'GRADE_SIZING_ENABLED': 'GradeSize',
        'SKIP_DECIDED_MARKETS': 'SkipDecided',
    }

    @staticmethod
    def _fmt_num(v):
        """Compact number formatting for buttons/labels (ints w/o decimals)."""
        if isinstance(v, bool) or v is None:
            return str(v)
        if isinstance(v, int):
            return str(v)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        if f == int(f):
            return str(int(f))
        return f"{f:g}"

    def _settings_view(self, group: str = None):
        """Build (text, inline_keyboard) for ONE settings tab/group, so the
        panel stays browsable instead of one giant +/- wall."""
        from bot import settings_store
        bools, nums = settings_store.snapshot()
        groups = settings_store.GROUPS
        gid = group or self._SETTINGS_DEFAULT_GROUP
        g = next((x for x in groups if x['id'] == gid), groups[0])
        gid = g['id']
        bkeys, nkeys = settings_store.group_keys(gid)

        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        master = '🟢 ON' if bools.get('TRADING_ENABLED') else '🔴 OFF'
        text = (
            f"⚙️ <b>Bot Settings</b> · {mode}\n"
            f"Master trading: <b>{master}</b>\n"
            f"{'-'*30}\n"
            f"📂 <b>{self._esc(g['title'])}</b>\n"
        )
        if bkeys:
            text += "\n<b>Toggles</b>\n"
            for k in bkeys:
                text += f"  {'✅' if bools.get(k) else '❌'} {self._esc(self._LABELS.get(k, k))}\n"
        if nkeys:
            text += "\n<b>Gates</b>\n"
            for k in nkeys:
                text += f"  • {self._esc(k)} = <b>{self._fmt_num(nums.get(k))}</b>\n"
        text += "\n<i>Or type /set KEY VALUE · /toggle KEY</i>"

        rows = []
        # Tab row(s): 3 per row, the active tab marked with a dot.
        tab_row = []
        for x in groups:
            label = ('• ' if x['id'] == gid else '') + x['tab']
            tab_row.append({'text': label, 'callback_data': f"st:{x['id']}"})
            if len(tab_row) == 3:
                rows.append(tab_row)
                tab_row = []
        if tab_row:
            rows.append(tab_row)
        # Toggle buttons: 2 per row.
        for i in range(0, len(bkeys), 2):
            row = []
            for k in bkeys[i:i + 2]:
                on = bools.get(k)
                row.append({'text': f"{'✅' if on else '❌'} {self._LABELS.get(k, k)}",
                            'callback_data': f"tg:{k}:{gid}"})
            rows.append(row)
        # Numeric gates: one row each [➖ step][KEY = val][➕ step].
        for k in nkeys:
            step = settings_store.NUM_KEYS[k][2]
            v = nums.get(k)
            rows.append([
                {'text': f"➖{self._fmt_num(step)}", 'callback_data': f"dn:{k}:{gid}"},
                {'text': f"{k} = {self._fmt_num(v)}", 'callback_data': 'noop'},
                {'text': f"➕{self._fmt_num(step)}", 'callback_data': f"up:{k}:{gid}"},
            ])
        return text, {'inline_keyboard': rows}

    def send_settings(self, group: str = None, edit_message_id: int = None):
        text, kb = self._settings_view(group)
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def _handle_callback(self, data: str, callback_id: str, message_id):
        from bot import settings_store
        if not data or data == 'noop':
            self._answer_callback(callback_id)
            return

        # Positions pager/sorter: "pos:<page>:<sort>:<with_summary>"
        if data.startswith('pos:'):
            try:
                _, page_s, sort_key, sm = data.split(':')
                page = int(page_s)
            except (ValueError, IndexError):
                self._answer_callback(callback_id)
                return
            self._answer_callback(callback_id)
            self.send_positions(page=page, sort=sort_key,
                                with_summary=(sm == '1'),
                                edit_message_id=message_id)
            return

        # Settings tab switch: "st:<group_id>"
        if data.startswith('st:'):
            group = data.split(':', 1)[1]
            self._answer_callback(callback_id)
            self.send_settings(group=group, edit_message_id=message_id)
            return

        # Lifecycle action buttons: "act:start|settings|restart"
        if data.startswith('act:'):
            action = data.split(':', 1)[1]
            if action == 'start':
                from bot import settings_store
                settings_store.set_value('TRADING_ENABLED', True)
                self._restart_pending = False
                self._answer_callback(callback_id, 'Trading enabled')
                self.send("🟢 <b>Trading ENABLED</b> — the bot will place new trades.")
            elif action == 'settings':
                self._restart_pending = False
                self._answer_callback(callback_id)
                self.send_settings(edit_message_id=message_id)
            elif action == 'restart':
                self._answer_callback(callback_id)
                self._prompt_restart()
            elif action == 'restart_confirm':
                self._answer_callback(callback_id, 'Restarting fresh')
                self._do_restart()
            elif action == 'restart_cancel':
                self._restart_pending = False
                self._answer_callback(callback_id, 'Cancelled')
                self.send("✖️ Restart cancelled — positions untouched.")
            else:
                self._answer_callback(callback_id)
            return

        # Toggle / bump: "<action>:<KEY>[:<group>]"
        parts = data.split(':')
        action = parts[0]
        key = parts[1] if len(parts) > 1 else ''
        group = parts[2] if len(parts) > 2 else None
        if not key:
            self._answer_callback(callback_id)
            return
        ok, msg = False, 'no change'
        if action == 'tg':
            ok, msg = settings_store.toggle(key)
        elif action == 'up':
            ok, msg = settings_store.bump(key, +1)
        elif action == 'dn':
            ok, msg = settings_store.bump(key, -1)
        self._answer_callback(callback_id, msg)
        if ok and message_id is not None:
            self.send_settings(group=group, edit_message_id=message_id)

    def _handle_command(self, text: str):
        """Handle incoming bot commands."""
        cmd = text.lower().split()[0] if text else ''
        parts = text.split()

        if cmd in ('/start', '/resume', 'start'):
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', True)
            self.send("🟢 <b>Trading ENABLED</b> — the bot will place new trades.")
        elif cmd in ('/restart', 'restart'):
            self._prompt_restart()
        elif cmd == '/stop' or cmd == '/pause':
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', False)
            self.send("🔴 <b>Trading DISABLED</b> — monitoring & resolving only, no new buys.")
        elif cmd == '/settings' or cmd == '/config':
            grp = parts[1].lower() if len(parts) >= 2 else None
            self.send_settings(group=grp)
        elif cmd == '/set':
            from bot import settings_store
            if len(parts) >= 3:
                ok, msg = settings_store.set_value(parts[1], parts[2])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/set KEY VALUE</code>  e.g. <code>/set BASKET_MAX_COST 0.80</code>")
        elif cmd == '/toggle':
            from bot import settings_store
            if len(parts) >= 2:
                ok, msg = settings_store.toggle(parts[1])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/toggle KEY</code>  e.g. <code>/toggle SNIPER_ENABLED</code>")
        elif cmd == '/status' or cmd == '/stats':
            self.send_status()
        elif cmd == '/balance' or cmd == '/bal':
            bal = self.pm.get_balance() if self.pm else 0
            self.send(f"💰 Balance: ${bal:.2f}")
        elif cmd == '/pnl':
            pnl = self.pm.get_total_pnl() if self.pm else 0
            self.send(f"📊 Total PnL: ${pnl:+.2f}")
        elif cmd == '/positions' or cmd == '/pos':
            self.send_positions(page=0, sort='recent', with_summary=False)
        elif cmd == '/markets':
            self.send_markets_summary()
        elif cmd == '/analysis' or cmd == '/analyze' or cmd == '/report':
            self.send_analysis()
        elif cmd == '/redeem':
            if self.pm:
                count = self.pm.redeem_all_winning()
                # redeem_all_winning may return a count (int) or a list.
                n = len(count) if isinstance(count, list) else count
                self.notify_redeems_recent()
                self.send(f"💰 Redeemed {n} positions")
        elif cmd == '/help':
            self.send(
                "🌤️ <b>Weather Sniper Commands</b>\n"
                "<b>/start</b> — enable trading (or just type 'start')\n"
                "<b>/restart</b> — clear ALL positions & start fresh (or type 'restart')\n"
                "<b>/stop</b> — disable trading (monitor only)\n"
                "<b>/settings</b> — tabbed panel: toggle strategies & tune every gate\n"
                "   (e.g. <code>/settings peaker</code> opens that tab)\n"
                "/set KEY VALUE — set a gate, e.g. /set BASKET_MAX_COST 0.80\n"
                "/toggle KEY — flip a toggle, e.g. /toggle SNIPER_ENABLED\n"
                "/status — summary + positions (paged, sortable)\n"
                "/balance — current balance\n"
                "/pnl — total profit/loss\n"
                "/positions — open positions (10/page; sort by PnL/Losses/ROI/Recent)\n"
                "/markets — active weather markets\n"
                "/analysis — per-strategy performance + downloadable trade log\n"
                "/redeem — redeem winning positions\n"
                "/help — this message"
            )
        elif cmd.startswith('/'):
            self.send(f"❓ Unknown command. Try /help")
